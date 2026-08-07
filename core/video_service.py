"""
Agnes AI 视频生成及异步轮询逻辑
"""
import os
import time
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Video
from astrbot.core.message.message_event_result import MessageEventResult

from ..agnes_api import (
    AgnesVideoRequestConfig,
    generate_video_task,
    _get_agnes_session,
    _parse_error_body,
    AgnesAPIError,
)

class VideoService:
    """视频生成后台轮询任务与状态通知逻辑"""
    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def run_video_task(self, event: AstrMessageEvent, config: AgnesVideoRequestConfig):
        """后台异步轮询视频生成任务"""
        tmp_path: str = None
        send_info: str = None
        raw_size = 0
        send_error: Exception = None
        send_latency = 0.0
        api_latency = 0.0
        retries = 0

        t0 = time.monotonic()
        is_aioqhttp = event.get_platform_name() == "aiocqhttp"
        output_format = self.plugin.plugin_config.video_output_format
        auto_threshold_bytes = int(self.plugin.plugin_config.auto_threshold) * 1024 * 1024

        try:
            # 1. 提交任务并开始轮询
            video_url, api_latency = await generate_video_task(config)
            retries = 0

            if not video_url:
                raise Exception("Agnes 视频生成接口未返回有效的视频 URL")

            # 2. 发送结果
            if is_aioqhttp:
                if output_format == "url":
                    # URL 直传通道
                    mer = MessageEventResult()
                    mer.chain.append(Video.fromURL(video_url))
                    send_start = time.monotonic()
                    await event.send(mer)
                    send_latency += time.monotonic() - send_start
                    send_info = "URL直发"
                else:
                    # auto 模式下下载发送
                    dl_start = time.monotonic()
                    tmp_path, raw_size = await self._download_video_to_temp(video_url)
                    send_latency += time.monotonic() - dl_start

                    if raw_size >= auto_threshold_bytes:
                        # 视频太大走 file 发送，因为视频不支持 base64
                        mer = MessageEventResult().file_video(tmp_path)
                        send_start = time.monotonic()
                        await event.send(mer)
                        send_latency += time.monotonic() - send_start
                        send_info = f"file:// {raw_size/1024:.1f}KB"
                    else:
                        # 视频小文件也通过 file_video 直发
                        mer = MessageEventResult().file_video(tmp_path)
                        send_start = time.monotonic()
                        await event.send(mer)
                        send_latency += time.monotonic() - send_start
                        send_info = f"file:// {raw_size/1024:.1f}KB"
            else:
                # 非 QQ 平台
                if output_format == "url":
                    mer = MessageEventResult()
                    mer.chain.append(Video.fromURL(video_url))
                    send_start = time.monotonic()
                    await event.send(mer)
                    send_latency += time.monotonic() - send_start
                    send_info = "URL直发"
                else:
                    dl_start = time.monotonic()
                    tmp_path, raw_size = await self._download_video_to_temp(video_url)
                    send_latency += time.monotonic() - dl_start
                    mer = MessageEventResult().file_video(tmp_path)
                    send_start = time.monotonic()
                    await event.send(mer)
                    send_latency += time.monotonic() - send_start
                    send_info = f"file:// {raw_size/1024:.1f}KB"

        except asyncio.CancelledError:
            raise
        except Exception as e:
            send_error = e
            logger.error(f"[agnes] 视频生成或发送失败: {type(e).__name__}: {e}", exc_info=True)
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError as e:
                    logger.debug(f"[agnes] 清理临时文件失败（可忽略）: {e}")

        if send_error is not None:
            err_text = self.plugin._format_error_message(
                send_error,
                api_latency=api_latency,
                retries=retries,
                send_latency=time.monotonic() - t0 - api_latency,
            )
            # 区分：如果是生成成功但发送失败，提供视频下载 URL
            if "video_url" in locals() and video_url:
                err_text = (
                    f"❌ 视频发送失败：{type(send_error).__name__}\n"
                    f"🎬 视频已成功生成！链接: {video_url}"
                )
            await event.send(MessageEventResult().message(err_text))
            return

        # 成功统计发送
        line_parts = [
            f"🎬 视频生成成功",
            f"⏱ API响应 {api_latency:.1f}s",
            f"发送 {send_latency:.1f}s",
            f"重试 {retries}次",
        ]
        if send_info:
            line_parts.append(send_info)
        await event.send(MessageEventResult().message(" | ".join(line_parts)))

    async def _download_video_to_temp(self, download_url: str) -> Tuple[str, int]:
        cache_dir = self.plugin._cache_dir or Path(tempfile.gettempdir())
        async with aiohttp.ClientSession() as session:
            async with session.get(
                download_url,
                proxy=self.plugin.plugin_config.proxy or None,
                timeout=aiohttp.ClientTimeout(total=int(self.plugin.plugin_config.video_request_timeout)),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"下载视频失败 (HTTP {resp.status})")
                raw = await resp.read()
        
        import tempfile
        tmp_obj = tempfile.NamedTemporaryFile(
            prefix="agnes_video_",
            suffix=".mp4",
            dir=str(cache_dir),
            delete=False,
        )
        try:
            tmp_obj.write(raw)
            tmp_obj.flush()
        finally:
            tmp_obj.close()
        return tmp_obj.name, len(raw)
