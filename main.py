"""
astrbot_plugin_agnes_image 主入口

独立封装的 Agnes AI 图像生成插件。
- 指令：生图 / 改图 / Agnes帮助
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
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage, Video, Plain, BaseMessageComponent
from astrbot.core.message.components import ComponentType

class DirectUrlImage(BaseMessageComponent):
    type: ComponentType = ComponentType.Image
    file: str
    def __init__(self, url: str):
        super().__init__(file=url)
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools

from .agnes_api import (
    AgnesAPIError,
    AgnesRequestConfig,
    AgnesVideoRequestConfig,
    PRESET_ASPECT_RATIOS,
    PRESET_RESOLUTIONS,
    PRESET_QUALITIES,
    _close_agnes_sessions,
    _gcd,
    generate_image,
    generate_video_task,
)
from .napcat_stream import upload_file_stream

# 插件名（用于解析 plugin_data 目录）
_PLUGIN_NAME = "astrbot_plugin_agnes_image"
# 临时图片缓存目录名
_CACHE_SUBDIR = "cache"
# 启动时清理超过 1 小时的临时文件
_CACHE_MAX_AGE_SECONDS = 3600

# AstrBot 文件服务补丁状态：让 /api/file/<token> 在有效期内可被 Agnes 预检和生成阶段重复访问。
_AGNES_FILE_SERVICE_MAGIC_PATCHED = False


# Agnes 支持的图像生成模型（来自 /v1/models 实测，仅保留生图模型）
AGNES_MODELS = [
    "agnes-image-2.1-flash",
    "agnes-image-2.0-flash",
]


def _is_agnes_ai(api_base: str, model: str) -> bool:
    base_lower = (api_base or "").lower()
    model_lower = (model or "").lower()
    return "agnes-ai" in base_lower or model_lower.startswith("agnes-image")


class AgnesImagePlugin(Star):
    """Agnes AI 图像生成插件"""

    def _is_public_url(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        if "127.0.0.1" in url_lower or "localhost" in url_lower or "/api/files/content" in url_lower or "/files/content" in url_lower or "qpic.cn" in url_lower or "gtimg.cn" in url_lower or "qq.com" in url_lower:
            return False
        return url_lower.startswith("http://") or url_lower.startswith("https://")

    async def _upload_to_public_host(self, file_path: str) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                filename = os.path.basename(file_path)
                with open(file_path, 'rb') as f:
                    data.add_field('file', f, filename=filename)
                    async with session.post('https://telegra.ph/upload', data=data) as resp:
                        if resp.status == 200:
                            res_json = await resp.json()
                            if isinstance(res_json, list) and len(res_json) > 0 and 'src' in res_json[0]:
                                return f"https://telegra.ph{res_json[0]['src']}"
        except Exception as e:
            logger.warning(f"[agnes] Telegraph 上传失败: {e}，尝试 Catbox...")

        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field('reqtype', 'fileupload')
                with open(file_path, 'rb') as f:
                    data.add_field('fileToUpload', f)
                    async with session.post('https://catbox.moe/user/api.php', data=data) as resp:
                        if resp.status == 200:
                            res_text = await resp.text()
                            if res_text.startswith("http"):
                                return res_text.strip()
        except Exception as e:
            logger.error(f"[agnes] Catbox 上传也失败: {e}")
        raise Exception("公网图床上传失败，请检查网络或代理设置。")

    async def _upload_to_third_party(self, file_path: str, upload_url: str, token: str) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                filename = os.path.basename(file_path)
                with open(file_path, 'rb') as f:
                    data.add_field('image', f, filename=filename)
                    headers = {}
                    if token:
                        headers['Authorization'] = f"Bearer {token}"
                    params = {}
                    if "imgbb" in upload_url.lower() and token:
                        params['key'] = token
                    
                    async with session.post(upload_url, data=data, headers=headers, params=params) as resp:
                        if resp.status == 200:
                            res_json = await resp.json()
                            found_url = self._find_url_in_json(res_json)
                            if found_url:
                                return found_url
                            raise Exception(f"未在响应中找到图片 URL: {res_json}")
                        else:
                            raise Exception(f"HTTP {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"[agnes] 第三方图床上传失败: {e}")
            raise e

    async def _extract_video_reference_images(self, event: AstrMessageEvent) -> tuple[list[str], list[str]]:
        refs = []
        notices = []
        for comp in event.get_messages():
            if not isinstance(comp, AstrImage):
                continue

            comp_url = (getattr(comp, "url", None) or "").strip()
            if self._is_public_url(comp_url):
                refs.append(comp_url)
                continue

            file_field = (getattr(comp, "file", None) or "").strip()
            if self._is_public_url(file_field):
                refs.append(file_field)
                continue

            method = self.config.get("video_img_handling_method", "free_public")

            if method == "astrbot":
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        from astrbot.core import file_token_service
                        token = await file_token_service.register_file(file_path)

                        base_url = self.config.get("video_file_service_base_url", "").strip().rstrip("/")
                        if not base_url:
                            from astrbot.core.config.config import astrbot_config
                            base_url = astrbot_config.get("callback_api_base", "").strip().rstrip("/")

                        if not base_url:
                            raise Exception("未配置插件的“AstrBot文件服务公网地址”，且全局 callback_api_base 也为空")

                        public_url = f"{base_url}/api/file/{token}"
                        refs.append(public_url)
                        logger.info(f"[agnes] 成功通过 AstrBot 本地文件服务生成公网链接: {public_url}")
                        notices.append("🌸 已通过 AstrBot 文件服务成功生成参考图公网链接！")
                        continue
                    except Exception as e:
                        logger.error(f"[agnes] 使用 AstrBot 本地文件服务转换失败: {e}")
                        raise Exception(f"AstrBot 本地文件服务转换失败: {e}")

            elif method == "third_party":
                upload_url = self.config.get("third_party_upload_url", "").strip()
                token = self.config.get("third_party_token", "").strip()
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        if not upload_url:
                            raise Exception("未配置第三方图床上传 API 地址")
                        uploaded_url = await self._upload_to_third_party(file_path, upload_url, token)
                        refs.append(uploaded_url)
                        logger.info(f"[agnes] 成功将本地参考图上传至自定义第三方图床: {uploaded_url}")
                        notices.append("🌸 已将本地参考图成功上传至第三方图床！")
                        continue
                    except Exception as e:
                        logger.warning(f"[agnes] 自定义第三方图床上传失败 ({e})，正在自动回退到免费公网图床...")
                        try:
                            file_path = await comp.convert_to_file_path()
                            uploaded_url = await self._upload_to_public_host(file_path)
                            refs.append(uploaded_url)
                            logger.info(f"[agnes] 成功回退并上传至免费公网图床: {uploaded_url}")
                            notices.append("🌸 已回退并将本地参考图成功上传至免费公网图床！")
                            continue
                        except Exception as fallback_err:
                            logger.error(f"[agnes] 回退免费公网图床也失败了: {fallback_err}")

            else:
                if file_field or comp_url:
                    try:
                        file_path = await comp.convert_to_file_path()
                        uploaded_url = await self._upload_to_public_host(file_path)
                        refs.append(uploaded_url)
                        logger.info(f"[agnes] 成功将本地参考图上传至免费公网图床: {uploaded_url}")
                        notices.append("🌸 已将本地参考图成功上传至免费公网图床！")
                        continue
                    except Exception as e:
                        logger.error(f"[agnes] 免费公网图床上传失败: {e}")

            raise Exception(
                "Agnes 视频生成只支持公网图片 URL。\n"
                "插件尝试使用你选择的传输方式处理图片，但均未成功。\n"
                "💡 建议：检查服务器网络、代理配置，或者直接在聊天框发送图片的公网链接（如 http://...）。"
            )
        return refs, notices
    """Agnes AI 图像生成插件"""

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.config = config or {}
        self._cache_dir: Path | None = None

    # ===== 生命周期 =====

    def _install_astrbot_file_service_magic(self):
        """让 AstrBot 文件服务 token 在有效期内可重复访问。

        AstrBot 原生 FileTokenService.handle_file() 会在第一次访问时 pop 掉 token。
        Agnes-Video-V2.0 在图生视频时可能先进行轻量预检，再由生成后端真正下载图片；
        如果 token 被预检消耗，后续下载会拿到 404 HTML，进而导致 Internal generation failed。
        """
        global _AGNES_FILE_SERVICE_MAGIC_PATCHED

        if _AGNES_FILE_SERVICE_MAGIC_PATCHED:
            logger.info("[agnes] AstrBot 文件服务可重复访问补丁已安装，跳过重复安装")
            return

        if not self.config.get("video_enable_astrbot_file_magic", True):
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

                    # 关键：不 pop token，让 Agnes 的预检请求和实际生成下载都能在有效期内读取同一文件。
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
        # 启动时清理超过 1 小时的临时文件（兜底）
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
        """清理 cache 目录中超过 _CACHE_MAX_AGE_SECONDS 的残留临时文件。"""
        if not self._cache_dir or not self._cache_dir.exists():
            return
        try:
            now = time.time()
            purged = 0
            for fp in self._cache_dir.iterdir():
                if not fp.is_file():
                    continue
                try:
                    if now - fp.stat().st_mtime > _CACHE_MAX_AGE_SECONDS:
                        fp.unlink(missing_ok=True)
                        purged += 1
                except OSError:
                    continue
            if purged:
                logger.info(f"[agnes] 启动时清理 {purged} 个过期临时文件")
        except Exception as e:
            logger.debug(f"[agnes] 清理 cache 失败（可忽略）: {e}")

    def _build_config(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        model: str | None = None,
        quality: str | None = None,
    ) -> AgnesRequestConfig:
        """从插件配置构建请求配置"""
        final_quality = quality or self.config.get("default_quality", "auto")
        final_prompt = prompt
        if final_quality and final_quality != "auto":
            final_prompt = f"{prompt}, quality: {final_quality}"

        return AgnesRequestConfig(
            api_base=self.config.get("api_base", "https://apihub.agnes-ai.com/v1"),
            api_key=self.config.get("api_key", ""),
            model=model or self.config.get("model", "agnes-image-2.1-flash"),
            prompt=final_prompt,
            resolution=resolution or self.config.get("default_resolution", "2K"),
            aspect_ratio=aspect_ratio or self.config.get("default_aspect_ratio", "1:1"),
            reference_images=reference_images or [],
            proxy=self.config.get("proxy", "") or None,
            timeout=int(self.config.get("request_timeout", 300)),
            output_format=self.config.get("output_format", "url"),
        )

    async def _extract_reference_images(
        self, event: AstrMessageEvent
    ) -> list[str]:
        """从消息中提取参考图，优先保留公网 URL，否则规范化为 data URI"""
        refs: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, AstrImage):
                continue
            
            # 1. 优先检查 comp.url 是否为公网 URL
            comp_url = (getattr(comp, "url", None) or "").strip()
            if comp_url and (comp_url.startswith("http://") or comp_url.startswith("https://")):
                refs.append(comp_url)
                continue
                
            # 2. 检查 comp.file 是否为公网 URL
            file_field = (getattr(comp, "file", None) or "").strip()
            if file_field and (file_field.startswith("http://") or file_field.startswith("https://")):
                refs.append(file_field)
                continue
                
            # 3. 如果是本地路径，尝试注册到文件服务以获取公网 URL
            if file_field:
                try:
                    from astrbot.core.config.config import astrbot_config
                    callback_host = astrbot_config.get("callback_api_base")
                    if callback_host:
                        public_url = await comp.register_to_file_service()
                        if public_url and (public_url.startswith("http://") or public_url.startswith("https://")):
                            refs.append(public_url)
                            continue
                except Exception as e:
                    logger.debug(f"[agnes] 尝试注册文件服务失败: {e}")
                    
            # 4. 兜底：转换成 base64 Data URI
            if file_field:
                try:
                    data_uri = await self._normalize_image_to_data_uri(file_field)
                    if data_uri:
                        refs.append(data_uri)
                except Exception as e:
                    logger.warning(f"[agnes] 提取参考图失败: {e}")
                    continue
        return refs

    def _extract_reference_image_paths(self, event: AstrMessageEvent) -> list[str]:
        """从消息中提取参考图的原始 file 字段（保留原始路径/URL/前缀）。

        返回原始字符串列表，方便后续读取尺寸等元数据。
        """
        paths: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, AstrImage):
                continue
            file_field = (getattr(comp, "file", None) or "").strip()
            if file_field:
                paths.append(file_field)
        return paths

    async def _read_image_dimensions(self, image_input: str) -> tuple[int, int] | None:
        """读取图片宽高 (width, height)，失败返回 None。

        支持：data URI、base64:// 头、裸本地路径、file:// 头、http(s) URL、裸 base64。
        """
        try:
            from PIL import Image
            import io as _io
            import base64 as _b64_local

            s = (image_input or "").strip()
            if not s:
                return None

            raw_bytes: bytes | None = None

            # 1. data URI
            if s.startswith("data:"):
                if "," in s:
                    raw_bytes = _b64_local.b64decode(s.split(",", 1)[1])

            # 2. base64://
            elif s.startswith("base64://"):
                try:
                    raw_bytes = _b64_local.b64decode("".join(s[len("base64://"):].split()))
                except Exception:
                    raw_bytes = None

            # 3. 裸本地路径
            elif s.startswith("/") or (len(s) >= 3 and s[1] == ":" and s[2] in ("/", "\\")):
                path = Path(s)
                if path.is_file():
                    img = Image.open(path)
                    return img.size  # (width, height)

            # 4. file://
            elif s.startswith("file://"):
                path = Path(s[len("file://"):])
                if path.is_file():
                    img = Image.open(path)
                    return img.size

            # 5. http(s)://
            elif s.startswith(("http://", "https://")):
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=20)
                    ) as session:
                        async with session.get(s) as resp:
                            if resp.status == 200:
                                raw_bytes = await resp.read()
                except Exception as e:
                    logger.warning(f"[agnes] 下载参考图以读尺寸失败: {e}")

            # 6. 裸 base64 兜底
            if raw_bytes is None:
                try:
                    raw_bytes = _b64_local.b64decode("".join(s.split()))
                except Exception:
                    return None

            if raw_bytes is None:
                return None

            img = Image.open(_io.BytesIO(raw_bytes))
            return img.size  # (width, height)
        except Exception as e:
            logger.warning(f"[agnes] 读取图片尺寸失败: {e}")
            return None

    @staticmethod
    def _adjust_size_to_16_multiple(w: int, h: int) -> tuple[int, int]:
        """把尺寸调整为 16 的倍数（Agnes 要求）。

        优先保证宽高比不变；超长边按比例缩到 ≤ 3840。
        """
        # 四舍五入到最近的 16 倍数
        w_r = max(64, round(w / 16) * 16)
        h_r = max(64, round(h / 16) * 16)
        # 限制最长边 ≤ 3840
        if max(w_r, h_r) > 3840:
            scale = 3840 / max(w_r, h_r)
            w_r = max(64, round((w_r * scale) / 16) * 16)
            h_r = max(64, round((h_r * scale) / 16) * 16)
        return w_r, h_r

    @staticmethod
    def _compute_aspect_ratio(w: int, h: int) -> str | None:
        """根据图片宽高找出最接近的预设长宽比字符串。

        化简 (w, h) → (rw, rh)，与 PRESET_ASPECT_RATIOS 里所有比例比小数，
        返回差距最小的预设。失败返回 None。
        """
        if w <= 0 or h <= 0:
            return None
        # 化简为最简整数比
        g = _gcd(w, h)
        rw, rh = w // g, h // g
        # 在预设中找比值最接近的
        target = rw / rh
        best: str | None = None
        best_diff = float("inf")
        for ratio in PRESET_ASPECT_RATIOS:
            a, b = ratio.split(":")
            a_i, b_i = int(a), int(b)
            if a_i <= 0 or b_i <= 0:
                continue
            v = a_i / b_i
            diff = abs(v - target)
            if diff < best_diff:
                best_diff = diff
                best = ratio
        return best

    async def _normalize_image_to_data_uri(self, image_input: str) -> str:
        """把任意形式的图片输入规范化为 ``data:image/...;base64,...``。

        支持：data URI、base64:// 头、file:// 头、http(s) URL、裸 base64。
        """
        s = (image_input or "").strip()
        if not s:
            return ""

        # 1. 已经是 data URI
        if s.startswith("data:"):
            return s

        # 2. AstrImage.file 在 aiocqhttp 等平台下会是 "base64://xxxx" 形式
        if s.startswith("base64://"):
            raw_b64 = s[len("base64://"):]
            # 去掉空白
            raw_b64 = "".join(raw_b64.split())
            return f"data:image/png;base64,{raw_b64}"

        # 3. 裸本地路径（aiocqhttp 实际表现：AstrImage.file 是 /AstrBot/data/temp/...）
        #    启发式：绝对路径且文件存在
        if (s.startswith("/") or (len(s) >= 3 and s[1] == ":" and s[2] in ("/", "\\"))):
            path = Path(s)
            if path.is_file():
                try:
                    mime = self._guess_mime_from_path(path)
                    data_b64 = _b64.b64encode(path.read_bytes()).decode("ascii")
                    return f"data:{mime};base64,{data_b64}"
                except Exception as e:
                    logger.warning(f"[agnes] 读取本地参考图失败: {path} err={e}")
                    return ""
            # 路径合法但文件不存在（被清理了）— 不当作裸 base64 兜底
            if path.parent.is_dir():
                logger.warning(f"[agnes] 本地参考图文件不存在: {path}")
                return ""

        # 4. 本地 file:// URI
        if s.startswith("file://"):
            path = Path(s[len("file://"):])
            if not path.exists():
                logger.warning(f"[agnes] 参考图文件不存在: {path}")
                return ""
            mime = self._guess_mime_from_path(path)
            data_b64 = _b64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data_b64}"

        # 5. 公网 URL → 下载后转 base64
        if s.startswith("http://") or s.startswith("https://"):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as session:
                    async with session.get(s) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"[agnes] 下载参考图失败 status={resp.status} url={s[:80]}"
                            )
                            return ""
                        raw = await resp.read()
                        ct = (resp.headers.get("Content-Type") or "image/png").split(";")[0]
                        mime = ct if ct.startswith("image/") else "image/png"
                        data_b64 = _b64.b64encode(raw).decode("ascii")
                        return f"data:{mime};base64,{data_b64}"
            except Exception as e:
                logger.warning(f"[agnes] 下载参考图异常: {e}")
                return ""

        # 6. 裸 base64（启发式判断，避免误把短字符串当图片）
        # 长度至少 64 字符且符合 base64 字符集
        base64_charset = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789+/=\n\r\t "
        )
        if len(s) >= 64 and all(c in base64_charset for c in s):
            return f"data:image/png;base64,{"".join(s.split())}"

        # 都识别不了
        logger.warning(f"[agnes] 无法识别的参考图格式（前 50 字符）: {s[:50]}")
        return ""

    @staticmethod
    def _guess_mime_from_path(path: Path) -> str:
        """根据扩展名猜测 MIME"""
        ext = path.suffix.lower().lstrip(".")
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "image/png")

    def _extract_prompt(self, event: AstrMessageEvent, raw: str, prefixes: tuple[str, ...]) -> str:
        """从消息中提取纯净的 prompt（去除指令前缀）"""
        text = (raw or "").strip()
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text

    def _parse_options(self, raw: str) -> dict[str, Any]:
        """
        解析 prompt 中的内联选项，支持中文连写格式，如：
          生图 一只猫 尺寸2K 比例16:9 质量高 模型2.1 保留原尺寸
        同时也向下兼容原有的 --key value 格式。
        """
        # 中文别名映射表
        alias_map = {
            "尺寸": "res",
            "分辨率": "res",
            "比例": "ratio",
            "长宽比": "ratio",
            "质量": "quality",
            "模型": "model",
        }
        
        value_alias_map = {
            "高": "high",
            "中": "medium",
            "低": "low",
            "自动": "auto",
            "2.1": "agnes-image-2.1-flash",
            "2.0": "agnes-image-2.0-flash",
            "2.1flash": "agnes-image-2.1-flash",
            "2.0flash": "agnes-image-2.0-flash",
        }

        # 无值标志选项
        flag_aliases = {
            "keep_size": "keep_size",
            "keepsize": "keep_size",
            "keep-size": "keep_size",
            "same_size": "keep_size",
            "samesize": "keep_size",
            "保留原尺寸": "keep_size",
            "保留尺寸": "keep_size",
            "原尺寸": "keep_size",
            "原比例": "keep_size",
        }

        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

        opts: dict[str, Any] = {}
        clean_tokens: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            
            # 1. 处理无值标志 (如：保留原尺寸)
            if tok in flag_aliases:
                opts["keep_size"] = True
                i += 1
                continue
                
            # 2. 处理标准的 --key value
            if tok.startswith("--"):
                raw_key = tok[2:].lower().replace("-", "_")
                if raw_key in flag_aliases:
                    opts[flag_aliases[raw_key]] = True
                    i += 1
                elif i + 1 < len(tokens):
                    val = tokens[i + 1]
                    opts[raw_key] = val
                    i += 2
                else:
                    i += 1
                continue
                
            # 3. 处理中文连写 (如：尺寸2K, 比例16:9, 质量高, 模型2.1)
            matched_alias = False
            for cn_key, en_key in alias_map.items():
                if tok.startswith(cn_key):
                    val = tok[len(cn_key):].strip()
                    if val:
                        # 翻译中文值
                        val = value_alias_map.get(val, val)
                        # 特殊处理分辨率：图片档位用 1K/2K/4K，视频档位用 480p/720p/1080p
                        if en_key == "res":
                            val_lower = val.lower()
                            if val_lower.endswith("p"):
                                val = val_lower
                            else:
                                val = val.upper()
                        opts[en_key] = val
                        matched_alias = True
                        break
            
            if matched_alias:
                i += 1
                continue
                
            # 4. 如果都不是，就当做是 prompt 的一部分
            clean_tokens.append(tok)
            i += 1

        opts["prompt"] = " ".join(clean_tokens).strip()
        return opts
    # 默认阈值：图片 < 2MB → base64://；图片 >= 2MB → file://
    DEFAULT_INLINE_B64_THRESHOLD_BYTES = 2 * 1024 * 1024

    async def _send_image_result(
        self,
        event: AstrMessageEvent,
        result: dict[str, Any],
        is_img2img: bool = False,
    ):
        """发送图像结果到消息链（智能切换 base64 / file / NapCat Stream，或 URL 直发）"""
        b64 = result.get("b64_json")
        url = result.get("url")
        usage = result.get("usage", {}) or {}
        api_latency = result.get("api_latency", 0.0) or 0.0
        retries = result.get("retries", 0) or 0

        if not b64 and not url:
            yield event.plain_result("❌ Agnes 未返回任何图像。")
            return

        is_aioqhttp = event.get_platform_name() == "aiocqhttp"
        output_format = self.config.get("output_format", "url")
        auto_threshold_bytes = int(self.config.get("auto_threshold", 2)) * 1024 * 1024

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
                    proxy=self.config.get("proxy", "") or None,
                    timeout=aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 300))),
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
            stream_start = time.monotonic()
            uploaded_path = await upload_file_stream(event, path)
            if not uploaded_path:
                return False
            # upload_file_stream 返回的是 NapCat 侧临时文件路径，不能再用 file_image()，
            # 否则 AstrBot 会尝试在自身容器内读取该路径并触发 FileNotFoundError。
            # 这里复用 DirectUrlImage 的“原样 file 字段直传”能力，把路径直接交给 OneBot/NapCat。
            mer_stream = MessageEventResult()
            mer_stream.chain.append(DirectUrlImage(uploaded_path))
            await event.send(mer_stream)
            stream_latency = time.monotonic() - stream_start
            send_latency += stream_latency
            send_info = f"Stream直发 {raw_size/1024:.1f}KB"
            send_error = None
            return True

        try:
            # QQ / NapCat 场景下，4K 这类大图不能先普通发送再等超时；需要预判后直接 Stream。
            if is_aioqhttp and url:
                try:
                    if output_format == "url":
                        # 伙伴要求 url 模式恢复原样，不再做 HEAD 预判或 Stream 兜底，纯粹直发。
                        mer = MessageEventResult()
                        mer.chain.append(DirectUrlImage(url))
                        send_start = time.monotonic()
                        await event.send(mer)
                        send_latency += time.monotonic() - send_start
                        send_info = "URL直发"
                    else:
                        # auto 模式下，直接下载并尝试 Stream 发送大图，失败退回 file
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
                            # 小于阈值，走 base64
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
                # 非 QQ 平台或仅有 base64 的场景：保留标准组件/auto 分流。
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
                                proxy=self.config.get("proxy", "") or None,
                                timeout=aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 300))),
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

        if usage.get("total_tokens"):
            line_parts.append(
                f"Token {usage['total_tokens']}"
                f"（输入 {usage.get('prompt_tokens', 0)} "
                f"/ 输出 {usage.get('completion_tokens', 0)}）"
            )
        yield event.plain_result(" | ".join(line_parts))

    def _format_error_message(
        self,
        exc: BaseException,
        api_latency: float = 0.0,
        retries: int = 0,
        send_latency: float = 0.0,
    ) -> str:
        """把异常对象格式化为用户友好的错误消息。

        会智能识别：
        - AgnesAPIError（HTTP 状态码、错误码、错误信息）
        - 网络异常（超时、连接失败）
        - 其他通用异常
        """
        lines: list[str] = []

        if isinstance(exc, AgnesAPIError):
            status = exc.status
            # 1. 语义化的错误标题
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

            # 2. 错误码（如果可识别）
            if exc.error_code:
                lines.append(f"📄 错误码：`{exc.error_code}`")

            # 3. 错误信息（从 JSON 中提取）
            if exc.error_message:
                lines.append(f"💬 错误信息：{exc.error_message}")

            # 4. 截断的原始响应 body（仅在没有结构化错误信息时展示）
            if exc.body and not exc.error_message:
                preview = exc.body[:200]
                if len(exc.body) > 200:
                    preview += "…"
                lines.append(f"📦 响应预览：{preview}")
        else:
            # 网络异常、JSON 解析失败等
            lines.append("❌ Agnes 调用失败")
            err_type = type(exc).__name__
            err_str = str(exc)
            if len(err_str) > 200:
                err_str = err_str[:200] + "…"
            lines.append(f"💬 [{err_type}] {err_str}")

            # 异常类型识别 + 提示
            if "Timeout" in err_type:
                lines.append("💡 提示：请求超时，可稍后重试，或检查网络/api_base")
            elif "Connection" in err_type or "Connector" in err_type:
                lines.append("💡 提示：无法连接服务器，请检查 api_base 配置或网络")
            elif "SSL" in err_type or "SSL" in err_str:
                lines.append("💡 提示：SSL/TLS 握手失败，可尝试更换 api_base")

        # 5. 统计行（与成功时同款）
        lines.append(
            f"⏱ API响应 {api_latency:.1f}s | 发送 {send_latency:.1f}s"
        )

        return "\n".join(lines)


    def _validate_inline_opts(self, opts: dict[str, str]) -> str | None:
        """校验内联选项，返回错误信息或 None"""
        if "res" in opts and opts["res"] not in PRESET_RESOLUTIONS:
            return (
                f"❌ --res 仅支持 {'/'.join(PRESET_RESOLUTIONS)}，"
                f"当前值：{opts['res']}"
            )
        if "ratio" in opts and opts["ratio"] not in PRESET_ASPECT_RATIOS:
            return (
                f"❌ --ratio 仅支持 {'/'.join(PRESET_ASPECT_RATIOS)}，"
                f"当前值：{opts['ratio']}"
            )
        if "quality" in opts and opts["quality"] not in PRESET_QUALITIES:
            return (
                f"❌ --quality 仅支持 {'/'.join(PRESET_QUALITIES)}，"
                f"当前值：{opts['quality']}"
            )
        if "model" in opts and opts["model"] not in AGNES_MODELS:
            return (
                f"❌ --model 仅支持 {'/'.join(AGNES_MODELS)}，"
                f"当前值：{opts['model']}"
            )

        # 4K 仅 agnes-image-2.1-flash 支持；2.0 flash 不支持，直接拦截。
        selected_model = opts.get("model") or self.config.get("model", "agnes-image-2.1-flash")
        selected_res = opts.get("res") or self.config.get("default_resolution", "2K")
        if selected_res == "4K" and selected_model != "agnes-image-2.1-flash":
            return "❌ `4K` 仅支持 `agnes-image-2.1-flash`，请切换模型或改用 `--res 2K`。"

        return None

    # ===== 指令 =====

    @filter.command("生图")
    async def cmd_generate(self, event: AstrMessageEvent, prompt: str):
        """
        文生图：生图 <描述> [--res 1K/2K/4K] [--ratio 16:9 ...] [--quality auto/low/medium/high] [--model agnes-...]
        """
        t0 = time.monotonic()
        try:
            raw = self._extract_prompt(event, prompt, ("生图",))
            opts = self._parse_options(raw)
            clean_prompt = opts.pop("prompt", "")
            if not clean_prompt:
                yield event.plain_result(
                    "❌ 请提供生图描述，例如：\n"
                    "生图 一只坐在月亮上的猫\n"
                    "生图 一只猫 --res 2K --ratio 16:9"
                )
                return

            if not self.config.get("api_key"):
                yield event.plain_result("❌ 尚未配置 api_key，请先在插件配置中填写 Agnes AI 密钥。")
                return

            err = self._validate_inline_opts(opts)
            if err:
                yield event.plain_result(err)
                return

            # 如果消息里有图片，自动当作图生图
            try:
                ref_images = await self._extract_reference_images(event)
            except Exception as e:
                logger.error(f"[agnes] 生图提取参考图失败: {e}", exc_info=True)
                yield event.plain_result(f"❌ 生图提取参考图失败: {type(e).__name__}: {e}")
                return

            if ref_images:
                yield event.plain_result("🔍 检测到参考图，自动切换为图生图模式...\n🎨 正在调用 Agnes 进行图生图...")
            else:
                yield event.plain_result("🎨 正在调用 Agnes 生成图像...")

            try:
                cfg = self._build_config(
                    prompt=clean_prompt,
                    reference_images=ref_images,
                    resolution=opts.get("res"),
                    aspect_ratio=opts.get("ratio"),
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

            # 生图指令：有参考图就当图生图处理（图生图不显示 token）
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
        """
        图生图：改图 <描述> [--res ...] [--ratio ...] [--quality ...] [--model ...]

        保留参考图原比例：
          - 默认：读插件配置 keep_original_size（true/false）
          - 命令行临时覆盖：--keep-size / --keepsize / --same-size
          - 启用时按第一张参考图的原宽高比生图，分辨率仍走 --res / default_resolution
        """
        raw = self._extract_prompt(event, prompt, ("改图",))
        opts = self._parse_options(raw)
        clean_prompt = opts.pop("prompt", "")
        if not clean_prompt:
            yield event.plain_result(
                "❌ 请提供改图描述，例如：\n改图 把它变成赛博朋克风格 --ratio 16:9"
            )
            return

        if not self.config.get("api_key"):
            yield event.plain_result("❌ 尚未配置 api_key，请先在插件配置中填写 Agnes AI 密钥。")
            return

        ref_images = await self._extract_reference_images(event)
        if not ref_images:
            yield event.plain_result("❌ 改图模式需要参考图，请把图片和「改图 描述」一起发出来。")
            return

        # 校验时把 keep_size 弹出（不是值选项）
        # 优先级：命令行 --keep-size > 插件配置 keep_original_size
        inline_keep = bool(opts.pop("keep_size", False))
        config_keep = bool(self.config.get("keep_original_size", False))
        keep_size = inline_keep or config_keep
        err = self._validate_inline_opts(opts)
        if err:
            yield event.plain_result(err)
            return

        # --keep-size：读取第一张参考图的宽高比，覆盖 ratio
        # 分辨率仍走 default_resolution（命令行 --res 或配置）
        keep_aspect_ratio: str | None = None
        if keep_size:
            ref_paths = self._extract_reference_image_paths(event)
            if ref_paths:
                dim = await self._read_image_dimensions(ref_paths[0])
                if dim:
                    w0, h0 = dim
                    aspect = self._compute_aspect_ratio(w0, h0)
                    if aspect:
                        keep_aspect_ratio = aspect
                        source = (
                            "命令行" if inline_keep
                            else "插件配置（keep_original_size）"
                        )
                        # 改图静默模式：不发送启用提示，仅记录日志
                        logger.info(
                            f"[agnes] 改图保留比例已启用（{source}），"
                            f"原图 {w0}x{h0} → 比例 {aspect}（档位走配置）"
                        )
                    else:
                        yield event.plain_result(
                            "⚠️ 保留比例启用失败：无法解析参考图宽高比，将按 --ratio 生成"
                        )
                else:
                    yield event.plain_result(
                        "⚠️ 保留比例启用失败：无法读取参考图尺寸，将按 --ratio 生成"
                    )
            else:
                yield event.plain_result(
                    "⚠️ 保留比例启用失败：未找到参考图，将按 --ratio 生成"
                )

        yield event.plain_result("🎨 正在调用 Agnes 进行图生图...")

        # 优先级：保留比例 > 命令行 --ratio > 配置 default_aspect_ratio
        # 保留比例启用时，--res 配置（default_resolution）仍生效
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

        # 改图指令：必然是图生图
        async for out in self._send_image_result(event, result, is_img2img=True):
            yield out



    # ==========================================
    # 视频生成指令
    # ==========================================

    @filter.command("生视频")
    async def cmd_generate_video(self, event: AstrMessageEvent, prompt: str):
        '''
        调用 Agnes-Video-V2.0 生成视频
        用法：生视频 <描述> [--res 480p/720p/1080p] [--ratio 16:9/9:16/1:1/4:3/3:4]（支持回复图片进行图生视频）
        '''
        raw = self._extract_prompt(event, prompt, ("生视频",))
        opts = self._parse_options(raw)
        clean_prompt = opts.pop("prompt", "")
        async for out in self._process_video_request(event, clean_prompt, opts):
            yield out

    async def _process_video_request(
        self, event: AstrMessageEvent, prompt: str, opts: dict[str, Any]
    ):
        if not prompt:
            yield event.plain_result("❌ 缺少描述！请提供视频描述。")
            return

        api_key = self.config.get("api_key", "")
        if not api_key:
            yield event.plain_result("❌ 未配置 API Key，请在插件设置中填写。")
            return

        # 视频配置
        video_model = self.config.get("video_model", "agnes-video-v2.0")
        video_duration = self.config.get("video_default_duration", "5s")
        video_output_format = self.config.get("video_output_format", "url")

        # 1. 提取并转换参考图
        try:
            reference_images, convert_notices = await self._extract_video_reference_images(event)
        except Exception as e:
            logger.error(f"[agnes] 生视频提取参考图失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 生视频提取参考图失败: {e}")
            return
            
        is_img2img = len(reference_images) > 0

        # 2. 先提示调用与模式切换
        if is_img2img:
            yield event.plain_result("🌸 正在调用Agnes生成视频...\n🎬 检测到参考图，自动切换为图生视频模式...")
        else:
            yield event.plain_result("🌸 正在调用Agnes生成视频...")
        
        # 内联选项校验与读取
        res = opts.get("res") or self.config.get("video_default_resolution", "720p")
        ratio = opts.get("ratio") or self.config.get("video_default_aspect_ratio", "16:9")
        
        keep_original_size = self.config.get("video_keep_original_size", False)
        if opts.get("keep_size") is True:
            keep_original_size = True
            
        if is_img2img and keep_original_size:
            res = "keep"
        
        if res not in ["480p", "720p", "1080p", "keep"]:
            yield event.plain_result(f"❌ 不支持的分辨率档位: {res}。支持: 480p/720p/1080p")
            return
        if ratio not in ["16:9", "9:16", "1:1", "4:3", "3:4"]:
            yield event.plain_result(f"❌ 不支持的长宽比: {ratio}。支持: 16:9/9:16/1:1/4:3/3:4")
            return
        
        # 提前读取原始图片尺寸（防止 agnes_api 内部重复下载消耗一次性 Token）
        img_w, img_h = None, None
        if is_img2img and keep_original_size:
            res = "keep"
            original_paths = self._extract_reference_image_paths(event)
            if original_paths:
                dims = await self._read_image_dimensions(original_paths[0])
                if dims:
                    img_w, img_h = dims
                    # 为了符合视频生成的常见要求，将宽高调整为16的倍数
                    def adjust_to_16(val: int) -> int:
                        return max(16, round(val / 16) * 16)
                    img_w = adjust_to_16(img_w)
                    img_h = adjust_to_16(img_h)
                    logger.info(f"[agnes] 图生视频保留原尺寸，提前读取尺寸: {img_w}x{img_h}")
        
        config = AgnesVideoRequestConfig(
            api_base=self.config.get("api_base", "https://apihub.agnes-ai.com/v1"),
            api_key=api_key,
            model=video_model,
            prompt=prompt,
            reference_images=reference_images,
            duration=video_duration,
            proxy=self.config.get("proxy", "") or None,
            timeout=int(self.config.get("video_request_timeout", 300)),
            output_format=video_output_format,
            resolution=res,
            aspect_ratio=ratio,
            width=img_w,
            height=img_h
        )

        submit_lines: list[str] = []
        submit_lines.extend(convert_notices)
        submit_lines.append(f"⏳ 视频生成任务已提交，预计需要几分钟（时长: {video_duration}），请耐心等待...")
        await event.send(event.plain_result("\n".join(submit_lines)))
        
        # 启动后台任务
        asyncio.create_task(self._run_video_task(event, config))

    async def _run_video_task(self, event: AstrMessageEvent, config: AgnesVideoRequestConfig):
        tmp_path: str | None = None
        send_info: str | None = None
        send_success = False
        send_start = time.monotonic()
        
        try:
            video_url, api_latency = await generate_video_task(config)
            
            send_start = time.monotonic()
            
            # Determine sending method based on video_output_format
            video_output_format = self.config.get("video_output_format", "url")
            auto_threshold_bytes = int(self.config.get("auto_threshold", 2)) * 1024 * 1024
            
            if video_output_format == "url":
                # URL 直发
                mer = MessageEventResult()
                mer.chain.append(Video.fromURL(video_url))
                if event.get_platform_name() != "aiocqhttp":
                    # 其他平台（如 webchat）不支持原生 Video 组件时，附带文本链接以便点击
                    mer.chain.append(Plain(f"\n🎬 视频链接: {video_url}"))
                await event.send(mer)
                send_success = True
                send_info = "URL直发"
            else:
                # auto 模式（智能切换）
                # 先下载到本地
                cache_dir = self._cache_dir or Path(tempfile.gettempdir())
                tmp_path_obj = tempfile.NamedTemporaryFile(
                    prefix="agnes_video_",
                    suffix=".mp4",
                    dir=str(cache_dir),
                    delete=False,
                )
                tmp_path = tmp_path_obj.name
                tmp_path_obj.close()
                
                raw_bytes = b""
                async with aiohttp.ClientSession() as session:
                    async with session.get(video_url, proxy=config.proxy) as resp:
                        if resp.status == 200:
                            raw_bytes = await resp.read()
                            with open(tmp_path, 'wb') as f:
                                f.write(raw_bytes)
                        else:
                            await event.send(event.plain_result(f"❌ 视频生成成功，但下载失败 (HTTP {resp.status})\n{video_url}"))
                            return
                
                raw_size = len(raw_bytes)
                
                # QQ 平台对于大体积视频（如>2MB），如果走普通 base64 或 file:// 可能会被同步超时截断，
                # 因此统一增加 NapCat Stream 兜底处理（与图片逻辑对齐）。
                # 注意：目前 upload_file_stream 支持传视频，NapCat 识别后会返回临时路径。
                is_aioqhttp = event.get_platform_name() == "aiocqhttp"
                if is_aioqhttp and raw_size >= auto_threshold_bytes:
                    uploaded_path = await upload_file_stream(event, tmp_path)
                    if uploaded_path:
                        mer_stream = MessageEventResult()
                        # 因为 upload_file_stream 返回的是 NapCat 容器内的临时路径，
                        # AstrBot 本地可能读取不到，所以直接把路径通过 Video.fromURL 当作远端地址交给 OneBot
                        mer_stream.chain.append(Video.fromURL(uploaded_path))
                        await event.send(mer_stream)
                        send_success = True
                        send_info = f"Stream直发 {raw_size/1024:.1f}KB"
                    else:
                        mer = MessageEventResult()
                        mer.chain.append(Video.fromFileSystem(tmp_path))
                        await event.send(mer)
                        send_success = True
                        send_info = f"file:// {raw_size/1024:.1f}KB"
                else:
                    if raw_size < auto_threshold_bytes:
                        b64_str = _b64.b64encode(raw_bytes).decode('utf-8')
                        mer = MessageEventResult()
                        mer.chain.append(Video.fromBase64(b64_str))
                        await event.send(mer)
                        send_success = True
                        send_info = f"base64 {raw_size/1024:.1f}KB"
                    else:
                        mer = MessageEventResult()
                        mer.chain.append(Video.fromFileSystem(tmp_path))
                        await event.send(mer)
                        send_success = True
                        send_info = f"file:// {raw_size/1024:.1f}KB"
            
            if send_success:
                send_latency = time.monotonic() - send_start
                line_parts = [
                    f"⏱ API响应 {api_latency:.1f}s",
                    f"发送 {send_latency:.1f}s"
                ]
                if send_info:
                    line_parts.append(send_info)
                await event.send(event.plain_result(" | ".join(line_parts)))
                
        except Exception as e:
            logger.error(f"[agnes] 视频任务异常: {e}", exc_info=True)
            err_type = type(e).__name__
            err_text = str(e)
            # 如果 video_url 已经存在，说明 Agnes 已经生成成功，异常发生在发送/下载阶段。
            if "video_url" in locals() and video_url:
                if "Timeout" in err_type:
                    msg = (
                        f"❌ 视频发送超时，但视频已生成成功。\n"
                        f"🎬 视频链接: {video_url}"
                    )
                else:
                    msg = (
                        f"❌ 视频发送失败: {err_type}: {err_text}\n"
                        f"🎬 视频已生成成功，链接: {video_url}"
                    )
            else:
                msg = f"❌ 视频生成失败: {err_type}: {err_text}"
            await event.send(event.plain_result(msg))
        finally:
            if tmp_path:
                try:
                    import os
                    # 延迟 10 秒清理视频文件，给 OneBot (NapCat) 充分的读取时间
                    async def delayed_delete(path_to_del: str):
                        await asyncio.sleep(10)
                        try:
                            os.remove(path_to_del)
                        except:
                            pass
                    asyncio.create_task(delayed_delete(tmp_path))
                except Exception as e:
                    logger.debug(f"[agnes] 延迟清理视频临时文件异常: {e}")

    @filter.command("Agnes帮助", alias={"agnes帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """显示 Agnes 插件使用说明"""
        help_text = (
            "🌸 Agnes 图像与视频生成插件使用说明\n\n"
            "📌 基础指令\n"
            "• 生图 <描述>：根据描述生成图像\n"
            "• 改图 <描述>：回复一张或多张图片后再发「改图 描述」，进行图生图或多图合成\n"
            "• 生视频 <描述>：生成视频（回复图片时自动切换为图生视频）\n\n"
            "🎛 快捷中文参数（直接连写在描述后即可）\n"
            "• 尺寸1K/2K/4K 或 尺寸480p/720p/1080p（视频）\n"
            "• 比例16:9 / 比例1:1 等\n"
            "• 质量高/中/低/自动（仅生图）\n"
            "• 模型2.1 / 模型2.0（仅生图）\n"
            "• 保留原尺寸（图生图/视频时保留参考图尺寸）\n\n"
            "✨ 示例\n"
            "• 生图 一只猫\n"
            "• 生图 赛博朋克城市夜景 尺寸4K 比例16:9 模型2.1\n"
            "• 改图 把它变成水彩画 比例1:1 质量高 保留原尺寸\n"
            "• 生视频 樱花飘落 尺寸1080p 比例16:9\n\n"
            "💡 小贴士\n"
            "• 兼容原有的 --res、--ratio 等英文参数格式\n"
            "• 配图为参考图时，生图 会自动切换为图生图，生视频 会自动切换为图生视频\n"
            "• 4K 仅支持 agnes-image-2.1-flash 模型\n"
        )
        yield event.plain_result(help_text)