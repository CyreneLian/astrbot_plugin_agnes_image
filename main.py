"""
Agnes AI 图像与视频生成插件主入口

依据 Agnes 官方文档进行了原生适配以实现完全免费、较高质量的定制化生成体验，支持文生图、图生图以及视频生成。
- 指令：生图 / 改图 / 生视频 / Agnes帮助
- 依赖：agnes_api.py 中的纯异步封装
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import shlex
import tempfile
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage, Video, Plain, BaseMessageComponent
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot.core.config import astrbot_config

# 本地模块导入
from .models.config import AgnesPluginConfig
from .core.image_service import ImageService
from .core.video_service import VideoService
from .core.uploader import Uploader, DirectUrlImage
from .agnes_api import (
    AgnesAPIError,
    AgnesRequestConfig,
    AgnesVideoRequestConfig,
    PRESET_ASPECT_RATIOS,
    PRESET_RESOLUTIONS,
    PRESET_QUALITIES,
    _close_agnes_sessions,
    generate_image,
)

# Agnes 支持的图像生成模型（来自 /v1/models 实测，仅保留生图模型）
AGNES_MODELS = [
    "agnes-image-2.1-flash",
    "agnes-image-2.0-flash",
]

# 插件名
_PLUGIN_NAME = "astrbot_plugin_agnes_image"
# 临时图片缓存目录名
_CACHE_SUBDIR = "cache"
# 启动时清理超过 1 小时的临时文件
_CACHE_MAX_AGE_SECONDS = 3600

# 补丁状态
_AGNES_FILE_SERVICE_MAGIC_PATCHED = False

@register(
    "astrbot_plugin_agnes_image",
    "往昔的涟漪",
    "Agnes AI 图像与视频生成插件，依据 Agnes 官方文档进行了原生适配以实现完全免费、较高质量的定制化生成体验，支持文生图、图生图以及视频生成。",
    "2.0.1",
    "https://github.com/CyreneLian/astrbot_plugin_agnes_image",
)
class AgnesImagePlugin(Star):
    """Agnes AI 图像生成插件"""
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        # 1. 配置模型化
        self.plugin_config = AgnesPluginConfig.from_dict(config or {})
        try:
            self.plugin_config.validate()
        except ValueError as e:
            logger.error(f"[agnes] 配置校验失败: {e}")

        # 兼容旧代码直接读取 self.config 字典
        self.config = config or {}
        self._cache_dir: Path | None = None

        # 2. 初始化核心逻辑服务层
        self.image_service = ImageService(self)
        self.video_service = VideoService(self)
        self.uploader = Uploader(self)

    # ===== 生命周期 =====

    def _install_astrbot_file_service_magic(self):
        """让 AstrBot 文件服务 token 在有效期内可重复访问。"""
        global _AGNES_FILE_SERVICE_MAGIC_PATCHED

        if _AGNES_FILE_SERVICE_MAGIC_PATCHED:
            logger.info("[agnes] AstrBot 文件服务可重复访问补丁已安装，跳过重复安装")
            return

        if not self.plugin_config.video_enable_astrbot_file_magic:
            logger.info("[agnes] AstrBot 文件服务可重复访问补丁未启用")
            return

        try:
            from astrbot.core import file_token_service

            if getattr(file_token_service, "_agnes_magic_patched", False):
                _AGNES_FILE_SERVICE_MAGIC_PATCHED = True
                logger.info("[agnes] AstrBot 文件服务可重复访问补丁已存在")
                return

            original_handle_file = file_token_service.handle_file

            async def agnes_repeatable_handle_file(file_token: str) -> str:
                async with file_token_service.lock:
                    await file_token_service._cleanup_expired_tokens()

                    if file_token not in file_token_service.staged_files:
                        raise KeyError(f"无效或过期的文件 token: {file_token}")

                    file_path, expire_time = file_token_service.staged_files[file_token]
                    if time.time() > expire_time:
                        file_token_service.staged_files.pop(file_token, None)
                        raise KeyError(f"无效或过期的文件 token: {file_token}")

                    if not os.path.exists(file_path):
                        file_token_service.staged_files.pop(file_token, None)
                        raise FileNotFoundError(f"文件不存在: {file_path}")

                    return file_path

            file_token_service._agnes_original_handle_file = original_handle_file
            file_token_service.handle_file = agnes_repeatable_handle_file
            file_token_service._agnes_magic_patched = True
            _AGNES_FILE_SERVICE_MAGIC_PATCHED = True
            logger.info("[agnes] 已安装 AstrBot 文件服务可重复访问补丁：/api/file token 有效期内不会因首次访问失效")
        except Exception as e:
            logger.error(f"[agnes] 安装 AstrBot 文件服务可重复访问补丁失败: {e}", exc_info=True)

    async def initialize(self):
        """插件加载：准备 cache 目录并清理历史临时文件。"""
        try:
            data_dir = StarTools.get_data_dir(_PLUGIN_NAME)
        except Exception as e:
            logger.error(f"[agnes] 无法解析 plugin_data 目录，回退到 /tmp: {e}")
            data_dir = Path(tempfile.gettempdir())
        self._cache_dir = data_dir / _CACHE_SUBDIR
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"[agnes] 创建 cache 目录失败，回退到 /tmp: {e}")
            self._cache_dir = Path(tempfile.gettempdir())
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        
        self._purge_stale_cache()
        logger.info(f"[agnes] cache 目录就绪: {self._cache_dir}")
        self._install_astrbot_file_service_magic()

    async def terminate(self):
        """插件卸载：关闭长连接 session。"""
        try:
            await _close_agnes_sessions()
        except Exception as e:
            logger.debug(f"[agnes] terminate 关闭 session 异常（可忽略）: {e}")

    # ===== 内部工具 =====

    def _purge_stale_cache(self):
        """清理 cache 目录中残留临时文件。"""
        if not self._cache_dir or not self._cache_dir.exists():
            return
        try:
            now = time.time()
            for fp in self._cache_dir.iterdir():
                if not fp.is_file():
                    continue
                try:
                    if now - fp.stat().st_mtime > _CACHE_MAX_AGE_SECONDS:
                        fp.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _build_config(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        quality: str | None = None,
        model: str | None = None,
    ) -> AgnesRequestConfig:
        """根据输入参数构建最终请求配置"""
        final_prompt = prompt
        q = quality or self.plugin_config.default_quality
        if q and q != "auto":
            final_prompt = f"{prompt}, {q} quality"

        return AgnesRequestConfig(
            api_base=self.plugin_config.api_base,
            api_key=self.plugin_config.api_key,
            model=model or self.plugin_config.model,
            prompt=final_prompt,
            resolution=resolution or self.plugin_config.default_resolution,
            aspect_ratio=aspect_ratio or self.plugin_config.default_aspect_ratio,
            reference_images=reference_images or [],
            proxy=self.plugin_config.proxy or None,
            timeout=int(self.plugin_config.request_timeout),
            output_format=self.plugin_config.output_format,
        )

    async def _extract_reference_images(
        self, event: AstrMessageEvent
    ) -> list[str]:
        """提取消息中的参考图并转换为 API 支持的格式（公网链接或 base64）"""
        refs = []
        for comp in self.image_service._get_all_message_components(event):
            if not isinstance(comp, AstrImage):
                continue
            
            comp_url = (getattr(comp, "url", None) or "").strip()
            if self.image_service._is_public_url(comp_url):
                refs.append(comp_url)
                continue

            file_field = (getattr(comp, "file", None) or "").strip()
            if self.image_service._is_public_url(file_field):
                refs.append(file_field)
                continue

            # 本地临时图片转 base64
            try:
                local_path = await comp.convert_to_file_path()
                with open(local_path, "rb") as f:
                    raw = f.read()
                b64_str = _b64.b64encode(raw).decode('utf-8')
                refs.append(b64_str)
            except Exception as e:
                logger.error(f"[agnes] 提取本地参考图失败: {e}", exc_info=True)
                raise Exception(f"提取参考图失败: {e}")
        return refs

    async def _extract_video_reference_images(
        self, event: AstrMessageEvent
    ) -> tuple[list[str], list[str], str | None, list[tuple[int, int] | None]]:
        """图生视频提取参考图"""
        refs = []
        notices = []
        status: str | None = None
        dims_list: list[tuple[int, int] | None] = []
        for comp in self.image_service._get_all_message_components(event):
            if not isinstance(comp, AstrImage):
                continue

            comp_url = (getattr(comp, "url", None) or "").strip()
            if self.image_service._is_public_url(comp_url):
                refs.append(comp_url)
                try:
                    local_path = await comp.convert_to_file_path()
                    dims = await self.image_service._read_image_dimensions(local_path)
                except Exception:
                    dims = await self.image_service._read_image_dimensions(comp_url)
                dims_list.append(dims)
                continue

            file_field = (getattr(comp, "file", None) or "").strip()
            if self.image_service._is_public_url(file_field):
                refs.append(file_field)
                try:
                    local_path = await comp.convert_to_file_path()
                    dims = await self.image_service._read_image_dimensions(local_path)
                except Exception:
                    dims = await self.image_service._read_image_dimensions(file_field)
                dims_list.append(dims)
                continue

            method = self.plugin_config.video_img_handling_method

            if method == "astrbot":
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        
                        # 核心修复：将临时文件复制到插件的 cache 目录，防止事件结束后被 AstrBot 框架的 event.cleanup 自动删除。
                        import shutil
                        import uuid
                        cache_dir = self._cache_dir or Path(tempfile.gettempdir())
                        safe_file_name = f"agnes_ref_{uuid.uuid4().hex}.png"
                        safe_file_path = cache_dir / safe_file_name
                        shutil.copy2(file_path, safe_file_path)
                        
                        from astrbot.core import file_token_service
                        token = await file_token_service.register_file(str(safe_file_path))

                        base_url = self.plugin_config.video_file_service_base_url.rstrip("/")
                        if not base_url:
                            base_url = astrbot_config.get("callback_api_base", "").strip().rstrip("/")

                        if not base_url:
                            raise Exception("未配置插件的“AstrBot文件服务公网地址”，且全局 callback_api_base 也为空")

                        public_url = f"{base_url}/api/file/{token}"
                        refs.append(public_url)
                        dims = await self.image_service._read_image_dimensions(str(safe_file_path))
                        dims_list.append(dims)
                        logger.info(f"[agnes] 成功通过 AstrBot 本地文件服务生成公网链接: {public_url}")
                        notices.append("🌸 已通过 AstrBot 文件服务成功生成参考图公网链接！")
                        status = "astrbot"
                        continue
                    except Exception as e:
                        logger.error(f"[agnes] 使用 AstrBot 本地文件服务转换失败: {e}")
                        raise Exception(f"AstrBot 本地文件服务转换失败: {e}")

            elif method == "third_party":
                upload_url = self.plugin_config.third_party_upload_url
                token = self.plugin_config.third_party_token
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        if not upload_url:
                            raise Exception("未配置第三方图床上传 API 地址")
                        uploaded_url = await self.uploader.upload_to_third_party(file_path, upload_url, token)
                        refs.append(uploaded_url)
                        dims = await self.image_service._read_image_dimensions(file_path)
                        dims_list.append(dims)
                        logger.info(f"[agnes] 本地图片成功上传至第三方图床: {uploaded_url}")
                        notices.append("🌸 本地参考图成功上传至第三方图床！")
                        status = "third_party"
                        continue
                    except Exception as e:
                        logger.warning(f"[agnes] 第三方图床上传失败: {e}，尝试回退到免费公网图床...")
                        try:
                            file_path = await comp.convert_to_file_path()
                            uploaded_url = await self.uploader.upload_to_public_host(file_path)
                            refs.append(uploaded_url)
                            dims = await self.image_service._read_image_dimensions(file_path)
                            dims_list.append(dims)
                            notices.append("🌸 第三方图床上传失败，已成功回退至免费公网图床！")
                            status = "fallback_public"
                            continue
                        except Exception as ex:
                            raise Exception(f"第三方图床上传失败且回退公网图床也失败: {ex}")

            else:
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        uploaded_url = await self.uploader.upload_to_public_host(file_path)
                        refs.append(uploaded_url)
                        dims = await self.image_service._read_image_dimensions(file_path)
                        dims_list.append(dims)
                        logger.info(f"[agnes] 本地图片成功上传至免费公网图床: {uploaded_url}")
                        notices.append("🌸 本地参考图已成功上传至免费公网图床！")
                        status = "public"
                        continue
                    except Exception as e:
                        raise Exception(f"上传至公网图床失败: {e}")

        return refs, notices, status, dims_list

    async def _send_image_result(
        self,
        event: AstrMessageEvent,
        result: dict[str, Any],
        is_img2img: bool = False,
    ):
        """发送图像结果到消息链"""
        b64 = result.get("b64_json")
        url = result.get("url")
        api_latency = result.get("api_latency", 0.0) or 0.0
        retries = result.get("retries", 0) or 0

        if not b64 and not url:
            yield event.plain_result("❌ Agnes 未返回任何图像。")
            return

        is_aioqhttp = event.get_platform_name() == "aiocqhttp"
        output_format = self.plugin_config.output_format
        auto_threshold_bytes = int(self.plugin_config.auto_threshold) * 1024 * 1024

        tmp_path: str | None = None
        send_info: str | None = None
        raw_size = 0
        send_error: Exception | None = None
        send_latency = 0.0

        async def _download_to_temp(download_url: str, prefix: str = "agnes_") -> tuple[str, int]:
            cache_dir = self._cache_dir or Path(tempfile.gettempdir())
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    download_url,
                    proxy=self.plugin_config.proxy or None,
                    timeout=aiohttp.ClientTimeout(total=int(self.plugin_config.request_timeout)),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"下载图片失败 (HTTP {resp.status})")
                    raw = await resp.read()
            tmp_obj = tempfile.NamedTemporaryFile(
                prefix=prefix,
                suffix=".png",
                dir=str(cache_dir),
                delete=False,
            )
            try:
                tmp_obj.write(raw)
                tmp_obj.flush()
            finally:
                tmp_obj.close()
            return tmp_obj.name, len(raw)

        async def _send_via_stream(path: str) -> bool:
            nonlocal send_info, send_latency, send_error
            from .napcat_stream import upload_file_stream
            stream_start = time.monotonic()
            uploaded_path = await upload_file_stream(event, path)
            if not uploaded_path:
                return False
            mer_stream = MessageEventResult()
            mer_stream.chain.append(DirectUrlImage(uploaded_path))
            await event.send(mer_stream)
            stream_latency = time.monotonic() - stream_start
            send_latency += stream_latency
            send_info = f"Stream直发 {raw_size/1024:.1f}KB"
            send_error = None
            return True

        try:
            if is_aioqhttp and url:
                try:
                    if output_format == "url":
                        mer = MessageEventResult()
                        mer.chain.append(DirectUrlImage(url))
                        send_start = time.monotonic()
                        await event.send(mer)
                        send_latency += time.monotonic() - send_start
                        send_info = "URL直发"
                    else:
                        dl_start = time.monotonic()
                        tmp_path, raw_size = await _download_to_temp(url, prefix="agnes_stream_")
                        send_latency += time.monotonic() - dl_start
                        
                        if raw_size >= auto_threshold_bytes:
                            ok = await _send_via_stream(tmp_path)
                            if not ok:
                                mer = MessageEventResult().file_image(tmp_path)
                                send_start = time.monotonic()
                                await event.send(mer)
                                send_latency += time.monotonic() - send_start
                                send_info = f"file:// {raw_size/1024:.1f}KB"
                        else:
                            with open(tmp_path, "rb") as f:
                                raw_bytes = f.read()
                            b64_str = _b64.b64encode(raw_bytes).decode('utf-8')
                            mer = MessageEventResult().base64_image(b64_str)
                            send_start = time.monotonic()
                            await event.send(mer)
                            send_latency += time.monotonic() - send_start
                            send_info = f"base64 {raw_size/1024:.1f}KB"
                except Exception as e:
                    send_error = e
                    logger.warning(f"[agnes] QQ 图片发送失败: {type(e).__name__}: {e}", exc_info=True)

            else:
                if output_format == "url" and url:
                    mer = MessageEventResult()
                    mer.chain.append(AstrImage.fromURL(url))
                    send_start = time.monotonic()
                    await event.send(mer)
                    send_latency += time.monotonic() - send_start
                    send_info = "URL直发"
                else:
                    if b64:
                        b64_data = b64
                        if b64.startswith("data:"):
                            b64_data = b64.split(",", 1)[1]
                        raw_bytes = _b64.b64decode(b64_data)
                    else:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                url,
                                proxy=self.plugin_config.proxy or None,
                                timeout=aiohttp.ClientTimeout(total=int(self.plugin_config.request_timeout)),
                            ) as resp:
                                if resp.status != 200:
                                    yield event.plain_result(f"❌ 下载图片失败 (HTTP {resp.status})")
                                    return
                                raw_bytes = await resp.read()
                    raw_size = len(raw_bytes)
                    if raw_size < auto_threshold_bytes:
                        b64_str = _b64.b64encode(raw_bytes).decode("utf-8")
                        mer = MessageEventResult().base64_image(b64_str)
                        send_info = f"base64 {raw_size/1024:.1f}KB"
                    else:
                        cache_dir = self._cache_dir or Path(tempfile.gettempdir())
                        tmp_obj = tempfile.NamedTemporaryFile(
                            prefix="agnes_",
                            suffix=".png",
                            dir=str(cache_dir),
                            delete=False,
                        )
                        try:
                            tmp_obj.write(raw_bytes)
                            tmp_obj.flush()
                        finally:
                            tmp_obj.close()
                        tmp_path = tmp_obj.name
                        mer = MessageEventResult().file_image(tmp_path)
                        send_info = f"file:// {raw_size/1024:.1f}KB"
                    send_start = time.monotonic()
                    await event.send(mer)
                    send_latency += time.monotonic() - send_start

        except asyncio.CancelledError:
            raise
        except Exception as e:
            send_error = e
            logger.error(f"[agnes] 图片发送阶段异常: {type(e).__name__}: {e}", exc_info=True)
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError as e:
                    logger.debug(f"[agnes] 清理临时文件失败（可忽略）: {e}")

        if send_error is not None:
            err_type = type(send_error).__name__
            err_text = str(send_error)
            if "Timeout" in err_type:
                msg = (
                    f"❌ 图片发送超时，但已生成成功。\n"
                    f"🖼️ 图片链接: {url}"
                )
            else:
                msg = (
                    f"❌ 图片发送失败: {err_type}: {err_text}\n"
                    f"🖼️ 图片已生成成功，链接: {url}"
                )
            yield event.plain_result(msg)
            return

        line_parts = [
            f"⏱ API响应 {api_latency:.1f}s",
            f"发送 {send_latency:.1f}s",
            f"重试 {retries}次",
        ]
        if send_info:
            line_parts.append(send_info)

        await event.send(MessageEventResult().message(" | ".join(line_parts)))

    def _format_error_message(
        self,
        exc: BaseException,
        api_latency: float = 0.0,
        retries: int = 0,
        send_latency: float = 0.0,
    ) -> str:
        lines: list[str] = []

        if isinstance(exc, AgnesAPIError):
            status = exc.status
            title_map = {
                400: "❌ 请求参数错误（HTTP 400）",
                401: "❌ API Key 无效或已过期（HTTP 401）",
                403: "❌ 没有访问权限（HTTP 403）",
                404: "❌ 接口地址不存在（HTTP 404）",
                413: "❌ 请求内容过大（HTTP 413）",
                422: "❌ 请求参数语义错误（HTTP 422）",
                429: "❌ 请求过快或配额已用完（HTTP 429）",
            }
            if status in title_map:
                lines.append(title_map[status])
            elif status and 500 <= status < 600:
                lines.append(f"❌ Agnes 服务端错误（HTTP {status}）")
            elif status:
                lines.append(f"❌ 请求失败（HTTP {status}）")
            else:
                lines.append("❌ Agnes 调用失败")

            if exc.error_code:
                lines.append(f"📄 错误码：`{exc.error_code}`")

            if exc.error_message:
                lines.append(f"💬 错误信息：{exc.error_message}")

            if exc.body and not exc.error_message:
                preview = exc.body[:200]
                if len(exc.body) > 200:
                    preview += "…"
                lines.append(f"📦 响应预览：{preview}")
        else:
            lines.append("❌ Agnes 调用失败")
            err_type = type(exc).__name__
            err_str = str(exc)
            if len(err_str) > 200:
                err_str = err_str[:200] + "…"
            lines.append(f"💬 [{err_type}] {err_str}")

            if "Timeout" in err_type:
                lines.append("💡 提示：请求超时，可稍后重试，或检查网络/api_base")
            elif "Connection" in err_type or "Connector" in err_type:
                lines.append("💡 提示：无法连接服务器，请检查 api_base 配置或网络")
            elif "SSL" in err_type or "SSL" in err_str:
                lines.append("💡 提示：SSL/TLS 握手失败，可尝试更换 api_base")

        lines.append(
            f"⏱ API响应 {api_latency:.1f}s | 发送 {send_latency:.1f}s"
        )

        return "\n".join(lines)

    def _validate_inline_opts(self, opts: dict[str, str]) -> str | None:
        if "res" in opts and opts["res"] not in PRESET_RESOLUTIONS:
            return f"❌ --res 仅支持 {'/'.join(PRESET_RESOLUTIONS)}，当前值：{opts['res']}"
        if "ratio" in opts and opts["ratio"] not in PRESET_ASPECT_RATIOS:
            return f"❌ --ratio 仅支持 {'/'.join(PRESET_ASPECT_RATIOS)}，当前值：{opts['ratio']}"
        if "quality" in opts and opts["quality"] not in PRESET_QUALITIES:
            return f"❌ --quality 仅支持 {'/'.join(PRESET_QUALITIES)}，当前值：{opts['quality']}"
        if "model" in opts and opts["model"] not in AGNES_MODELS:
            return f"❌ --model 仅支持 {'/'.join(AGNES_MODELS)}，当前值：{opts['model']}"

        selected_model = opts.get("model") or self.plugin_config.model
        selected_res = opts.get("res") or self.plugin_config.default_resolution
        if selected_res == "4K" and selected_model != "agnes-image-2.1-flash":
            return "❌ `4K` 仅支持 `agnes-image-2.1-flash`，请切换模型或改用 `--res 2K`。"

        return None

    # ===== 指令 =====

    @filter.command("生图")
    async def cmd_generate(self, event: AstrMessageEvent, prompt: str):
        """文生图指令"""
        t0 = time.monotonic()
        try:
            raw = self.image_service._extract_prompt(event, prompt, ("生图",))
            opts = self.image_service._parse_options(raw)
            clean_prompt = opts.pop("prompt", "")
            if not clean_prompt:
                yield event.plain_result(
                    "❌ 请提供生图描述，例如：\n"
                    "生图 一只坐在月亮上的猫\n"
                    "生图 一只猫 --res 2K --ratio 16:9"
                )
                return

            if not self.plugin_config.api_key:
                yield event.plain_result("❌ 尚未配置 api_key，请先在插件配置中填写 Agnes AI 密钥。")
                return

            inline_keep = bool(opts.pop("keep_size", False))
            config_keep = self.plugin_config.keep_original_size
            keep_size = inline_keep or config_keep

            err = self._validate_inline_opts(opts)
            if err:
                yield event.plain_result(err)
                return

            try:
                ref_images = await self._extract_reference_images(event)
            except Exception as e:
                logger.error(f"[agnes] 生图提取参考图失败: {e}", exc_info=True)
                yield event.plain_result(f"❌ 生图提取参考图失败: {type(e).__name__}: {e}")
                return

            keep_aspect_ratio: str | None = None
            if ref_images:
                yield event.plain_result("🔍 检测到参考图，自动切换为图生图模式...\n🎨 正在调用 Agnes 进行图生图...")
                
                if keep_size:
                    first_image = None
                    for comp in self.image_service._get_all_message_components(event):
                        if isinstance(comp, AstrImage):
                            first_image = comp
                            break
                    if first_image:
                        try:
                            local_path = await first_image.convert_to_file_path()
                            dim = await self.image_service._read_image_dimensions(local_path)
                        except Exception as e:
                            logger.warning(f"[agnes] 生图转换本地路径读取尺寸失败: {e}")
                            comp_url = getattr(first_image, "url", "") or getattr(first_image, "file", "")
                            dim = await self.image_service._read_image_dimensions(comp_url)
                        
                        if dim:
                            w0, h0 = dim
                            aspect = self.image_service._compute_aspect_ratio(w0, h0)
                            if aspect:
                                keep_aspect_ratio = aspect
                                source = "命令行" if inline_keep else "插件配置（keep_original_size）"
                                logger.info(f"[agnes] 生图保留比例已启用（{source}），原图 {w0}x{h0} → 比例 {aspect}（档位走配置）")
                            else:
                                yield event.plain_result("⚠️ 保留比例启用失败：无法解析参考图比例，将按 --ratio 生成。")
                        else:
                            err_detail = "未知原因"
                            try:
                                from PIL import Image
                                import urllib.request
                                test_target = local_path if 'local_path' in locals() else (getattr(first_image, "url", "") or getattr(first_image, "file", ""))
                                if test_target.startswith("file://"):
                                    path_str = urllib.request.url2pathname(test_target[len("file://"):])
                                    if os.name == 'nt' and path_str.startswith('/') and path_str[2] == ':':
                                        path_str = path_str[1:]
                                    p_obj = Path(path_str)
                                    if not p_obj.exists():
                                        err_detail = f"文件不存在: {path_str}"
                                    else:
                                        Image.open(p_obj)
                                elif test_target.startswith(("http://", "https://")):
                                    err_detail = f"网络链接无法在本地直接读取: {test_target}"
                                else:
                                    p_obj = Path(test_target)
                                    if not p_obj.exists():
                                        err_detail = f"本地文件不存在: {test_target}"
                                    else:
                                        Image.open(p_obj)
                            except Exception as ex:
                                err_detail = f"{type(ex).__name__}: {ex}"
                            
                            yield event.plain_result(
                                f"⚠️ 保留比例启用失败：无法读取参考图尺寸。\n"
                                f"🔍 输入参数: {test_target if 'test_target' in locals() else '无'}\n"
                                f"❌ 错误详情: {err_detail}\n"
                                f"将按 --ratio 生成。"
                            )
            else:
                yield event.plain_result("🎨 正在调用 Agnes 生成图像...")

            try:
                cfg = self._build_config(
                    prompt=clean_prompt,
                    reference_images=ref_images,
                    resolution=opts.get("res"),
                    aspect_ratio=keep_aspect_ratio or opts.get("ratio"),
                    quality=opts.get("quality"),
                    model=opts.get("model"),
                )
            except Exception as e:
                logger.error(f"[agnes] 生图配置构建失败: {e}", exc_info=True)
                yield event.plain_result(f"❌ 生图配置构建失败: {type(e).__name__}: {e}")
                return

            try:
                result = await generate_image(cfg)
            except Exception as e:
                logger.error(f"Agnes generate failed: {e}", exc_info=True)
                api_latency = getattr(e, "api_latency", 0.0) or 0.0
                yield event.plain_result(
                    self._format_error_message(
                        e,
                        api_latency=api_latency,
                        retries=getattr(e, "retries", 0) or 0,
                        send_latency=time.monotonic() - t0 - api_latency,
                    )
                )
                return

            is_img2img = bool(ref_images)
            try:
                async for out in self._send_image_result(event, result, is_img2img=is_img2img):
                    yield out
            except Exception as e:
                logger.error(f"[agnes] 生图发送结果阶段异常: {e}", exc_info=True)
                yield event.plain_result(f"❌ 生图发送结果阶段异常: {type(e).__name__}: {e}")
                return

        except Exception as e:
            logger.error(f"[agnes] cmd_generate 未捕获异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 生图指令异常: {type(e).__name__}: {e}")
            return

    @filter.command("改图")
    async def cmd_modify(self, event: AstrMessageEvent, prompt: str):
        """图生图指令"""
        raw = self.image_service._extract_prompt(event, prompt, ("改图",))
        opts = self.image_service._parse_options(raw)
        clean_prompt = opts.pop("prompt", "")
        if not clean_prompt:
            yield event.plain_result(
                "❌ 请提供改图描述，例如：\n改图 把它变成赛博朋克风格 --ratio 16:9"
            )
            return

        if not self.plugin_config.api_key:
            yield event.plain_result("❌ 尚未配置 api_key，请先在插件配置中填写 Agnes AI 密钥。")
            return

        ref_images = await self._extract_reference_images(event)
        if not ref_images:
            yield event.plain_result("❌ 改图模式需要参考图，请把图片和「改图 描述」一起发出来。")
            return

        inline_keep = bool(opts.pop("keep_size", False))
        config_keep = self.plugin_config.keep_original_size
        keep_size = inline_keep or config_keep

        err = self._validate_inline_opts(opts)
        if err:
            yield event.plain_result(err)
            return

        keep_aspect_ratio: str | None = None
        if keep_size:
            first_image = None
            for comp in self.image_service._get_all_message_components(event):
                if isinstance(comp, AstrImage):
                    first_image = comp
                    break
            if first_image:
                try:
                    local_path = await first_image.convert_to_file_path()
                    dim = await self.image_service._read_image_dimensions(local_path)
                except Exception as e:
                    logger.warning(f"[agnes] 改图转换本地路径读取尺寸失败: {e}")
                    comp_url = getattr(first_image, "url", "") or getattr(first_image, "file", "")
                    dim = await self.image_service._read_image_dimensions(comp_url)
                
                if dim:
                    w0, h0 = dim
                    aspect = self.image_service._compute_aspect_ratio(w0, h0)
                    if aspect:
                        keep_aspect_ratio = aspect
                        source = "命令行" if inline_keep else "插件配置（keep_original_size）"
                        logger.info(f"[agnes] 改图保留比例已启用（{source}），原图 {w0}x{h0} → 比例 {aspect}（分辨率仍按设置）")
                    else:
                        yield event.plain_result("⚠️ 保留比例启用失败：无法解析参考图比例，将按 --ratio 生成。")
                else:
                    err_detail = "未知原因"
                    try:
                        from PIL import Image
                        import urllib.request
                        test_target = local_path if 'local_path' in locals() else (getattr(first_image, "url", "") or getattr(first_image, "file", ""))
                        if test_target.startswith("file://"):
                            path_str = urllib.request.url2pathname(test_target[len("file://"):])
                            if os.name == 'nt' and path_str.startswith('/') and path_str[2] == ':':
                                path_str = path_str[1:]
                            p_obj = Path(path_str)
                            if not p_obj.exists():
                                err_detail = f"文件不存在: {path_str}"
                            else:
                                Image.open(p_obj)
                        elif test_target.startswith(("http://", "https://")):
                            err_detail = f"网络链接无法在本地直接读取: {test_target}"
                        else:
                            p_obj = Path(test_target)
                            if not p_obj.exists():
                                err_detail = f"本地文件不存在: {test_target}"
                            else:
                                Image.open(p_obj)
                    except Exception as ex:
                        err_detail = f"{type(ex).__name__}: {ex}"
                    
                    yield event.plain_result(
                        f"⚠️ 保留比例启用失败：无法读取参考图尺寸。\n"
                        f"🔍 输入参数: {test_target if 'test_target' in locals() else '无'}\n"
                        f"❌ 错误详情: {err_detail}\n"
                        f"将按 --ratio 生成。"
                    )
            else:
                yield event.plain_result(
                    "⚠️ 保留比例启用失败：未找到参考图，将按 --ratio 生成"
                )

        yield event.plain_result("🎨 正在调用 Agnes 进行图生图...")

        cfg = self._build_config(
            prompt=clean_prompt,
            reference_images=ref_images,
            resolution=opts.get("res"),
            aspect_ratio=keep_aspect_ratio or opts.get("ratio"),
            quality=opts.get("quality"),
            model=opts.get("model"),
        )
        t0 = time.monotonic()
        try:
            result = await generate_image(cfg)
        except Exception as e:
            logger.error(f"Agnes img2img failed: {e}", exc_info=True)
            api_latency = getattr(e, "api_latency", 0.0) or 0.0
            yield event.plain_result(
                self._format_error_message(
                    e,
                    api_latency=api_latency,
                    retries=getattr(e, "retries", 0) or 0,
                    send_latency=time.monotonic() - t0 - api_latency,
                )
            )
            return

        async for out in self._send_image_result(event, result, is_img2img=True):
            yield out

    @filter.command("生视频")
    async def cmd_generate_video(self, event: AstrMessageEvent, prompt: str):
        """生视频指令"""
        raw = self.image_service._extract_prompt(event, prompt, ("生视频",))
        opts = self.image_service._parse_options(raw)
        clean_prompt = opts.pop("prompt", "")
        async for out in self._process_video_request(event, clean_prompt, opts):
            yield out

    async def _process_video_request(
        self, event: AstrMessageEvent, prompt: str, opts: dict[str, Any]
    ):
        if not prompt:
            yield event.plain_result("❌ 缺少描述！请提供视频描述。")
            return

        api_key = self.plugin_config.api_key
        if not api_key:
            yield event.plain_result("❌ 未配置 API Key，请在插件设置中填写。")
            return

        video_model = self.plugin_config.video_model
        video_duration = self.plugin_config.video_default_duration
        video_output_format = self.plugin_config.video_output_format

        try:
            reference_images, convert_notices, ref_status, ref_dims = await self._extract_video_reference_images(event)
        except Exception as e:
            logger.error(f"[agnes] 生视频提取参考图失败: {e}", exc_info=True)
            await event.send(MessageEventResult().message(f"❌ 参考图转换失败：{e}"))
            return

        is_img2img = len(reference_images) > 0
        if is_img2img:
            yield event.plain_result("🌸 正在调用Agnes生成视频...\n🎬 检测到参考图，自动切换为图生视频模式...")
            second_msg = None
            if ref_status == "astrbot":
                second_msg = "🌸 已通过 AstrBot 文件服务成功生成参考图公网链接！\n⏳ 视频生成任务已提交，预计需要几分钟（时长: {0}），请耐心等待...".format(video_duration)
            elif ref_status == "third_party":
                second_msg = "🌸 已将本地参考图成功上传至第三方图床！\n⏳ 视频生成任务已提交，预计需要几分钟（时长: {0}），请耐心等待...".format(video_duration)
            elif ref_status == "fallback_public":
                second_msg = "🌸 已回退并将本地参考图成功上传至免费公网图床！\n⏳ 视频生成任务已提交，预计需要几分钟（时长: {0}），请耐心等待...".format(video_duration)
            elif ref_status == "public":
                second_msg = "🌸 已将本地参考图成功上传至免费公网图床！\n⏳ 视频生成任务已提交，预计需要几分钟（时长: {0}），请耐心等待...".format(video_duration)
            else:
                second_msg = f"⏳ 视频生成任务已提交，预计需要几分钟（时长: {video_duration}），请耐心等待..."
            await event.send(MessageEventResult().message(second_msg))
        else:
            yield event.plain_result("🌸 正在调用Agnes生成视频...\n⏳ 视频生成任务已提交，预计需要几分钟（时长: {0}），请耐心等待...".format(video_duration))

        res = opts.get("res") or self.plugin_config.video_default_resolution
        ratio = opts.get("ratio") or self.plugin_config.video_default_aspect_ratio
        
        keep_original_size = self.plugin_config.video_keep_original_size
        if opts.get("keep_size") is True:
            keep_original_size = True
            
        if is_img2img and keep_original_size:
            first_dim = None
            if ref_dims:
                for d in ref_dims:
                    if d:
                        first_dim = d
                        break
            if first_dim:
                w0, h0 = first_dim
                aspect = self.image_service._compute_aspect_ratio(w0, h0, ["16:9", "9:16", "1:1", "4:3", "3:4"])
                if aspect:
                    ratio = aspect
                    logger.info(f"[agnes] 视频保留比例已启用，原图 {w0}x{h0} -> 自动匹配比例: {aspect}（分辨率仍按设置）")
                else:
                    yield event.plain_result(f"⚠️ 视频保留比例失败：无法解析参考图比例 ({w0}x{h0})，将按默认/指令比例生成。")
            else:
                raw_target = "未知"
                if reference_images:
                    raw_target = reference_images[0]
                yield event.plain_result(
                    f"⚠️ 视频保留比例失败：无法读取参考图尺寸。\n"
                    f"🔍 输入参数: {raw_target}\n"
                    f"将按默认/指令比例生成。"
                )
        
        if res not in ["480p", "720p", "1080p"]:
            yield event.plain_result(f"❌ 不支持的分辨率档位: {res}。支持: 480p/720p/1080p")
            return
        if ratio not in ["16:9", "9:16", "1:1", "4:3", "3:4"]:
            yield event.plain_result(f"❌ 不支持的长宽比: {ratio}。支持: 16:9/9:16/1:1/4:3/3:4")
            return
        
        img_w, img_h = None, None
        
        config = AgnesVideoRequestConfig(
            api_base=self.plugin_config.api_base,
            api_key=api_key,
            model=video_model,
            prompt=prompt,
            reference_images=reference_images,
            duration=video_duration,
            proxy=self.plugin_config.proxy or None,
            timeout=int(self.plugin_config.video_request_timeout),
            output_format=video_output_format,
            resolution=res,
            aspect_ratio=ratio,
            width=img_w,
            height=img_h
        )

        asyncio.create_task(self.video_service.run_video_task(event, config))

    @filter.command("Agnes帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助"""
        help_text = """🎨 Agnes 图像与视频生成插件帮助 v2.0.1
━━━━━━━━━━━━
🌸 核心指令：
• 生图 <描述> - 生成图片
• 改图 <描述> - 图生图（需携带或引用图片）
• 生视频 <描述> - 生成视频（支持携带或引用图片）
• Agnes帮助 - 查看此菜单

💡 内联参数（直接跟在描述后）：
• 尺寸1K/2K/4K (生图) - 指定分辨率档
• 尺寸480p/720p/1080p (生视频)
• 比例16:9/9:16/1:1/4:3/3:4/3:2/2:3... - 长宽比
• 质量高/中/低/自动 - 附加质量词
• 模型2.1/2.0 - 指定 Agnes 模型
• 保留原比例 - 自动按参考图原比例生图/视频

📝 示例：
生图 一只粉色的小狐狸 尺寸2K 比例16:9 质量高
改图 把它变成赛博朋克风格 保留原比例
生视频 巨浪拍打礁石 尺寸720p 比例16:9
"""
        yield event.plain_result(help_text)
