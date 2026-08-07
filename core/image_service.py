"""
Agnes AI 图像与视频生成 逻辑处理层
"""
import os
import time
import shlex
import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as AstrImage
from astrbot.core.message.components import BaseMessageComponent

from ..agnes_api import (
    PRESET_ASPECT_RATIOS,
    PRESET_RESOLUTIONS,
    PRESET_QUALITIES,
    SIZE_PRESETS,
    _gcd,
)

class ImageService:
    """图像生图与改图的参数提取、尺寸比例计算业务逻辑"""
    def __init__(self, plugin: Any):
        self.plugin = plugin

    def _get_all_message_components(self, event: AstrMessageEvent) -> List[BaseMessageComponent]:
        """获取所有消息组件，若包含引用回复 (Reply)，则将 Reply.chain 中的组件优先放在最前面合并返回。"""
        from astrbot.core.message.components import Reply
        reply_comps = []
        normal_comps = []
        for comp in event.get_messages():
            if isinstance(comp, Reply) and comp.chain:
                for sub in comp.chain:
                    reply_comps.append(sub)
            else:
                normal_comps.append(comp)
        return reply_comps + normal_comps

    def _is_public_url(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        if "127.0.0.1" in url_lower or "localhost" in url_lower or "/api/files/content" in url_lower or "/files/content" in url_lower or "qpic.cn" in url_lower or "gtimg.cn" in url_lower or "qq.com" in url_lower:
            return False
        return url_lower.startswith("http://") or url_lower.startswith("https://")

    def _extract_prompt(self, event: AstrMessageEvent, raw: str, prefixes: Tuple[str, ...]) -> str:
        """从消息中提取纯净的 prompt（去除指令前缀）"""
        text = (raw or "").strip()
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text

    def _parse_options(self, raw: str) -> Dict[str, Any]:
        """解析 prompt 中的内联选项，支持中文连写格式与 --key value 格式。"""
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

        flag_aliases = {
            "keep_size": "keep_size",
            "keepsize": "keep_size",
            "keep-size": "keep_size",
            "same_size": "keep_size",
            "samesize": "keep_size",
            "保留原比例": "keep_size",
            "保留尺寸": "keep_size",
            "原比例": "keep_size",
        }

        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

        opts: Dict[str, Any] = {}
        clean_tokens: List[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in flag_aliases:
                opts["keep_size"] = True
                i += 1
                continue
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
            matched_alias = False
            for cn_key, en_key in alias_map.items():
                if tok.startswith(cn_key):
                    val = tok[len(cn_key):].strip()
                    if val:
                        val = value_alias_map.get(val, val)
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
            clean_tokens.append(tok)
            i += 1

        opts["prompt"] = " ".join(clean_tokens).strip()
        return opts

    async def _read_image_dimensions(self, image_input: str) -> Optional[Tuple[int, int]]:
        """读取图像的宽高尺寸"""
        s = (image_input or "").strip()
        if not s:
            return None
        if s.startswith(("http://", "https://")):
            try:
                import io
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(s, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            img = Image.open(io.BytesIO(data))
                            return img.size
            except Exception as e:
                logger.warning(f"[agnes] 读取网络图片尺寸失败: {e}")
                return None
        else:
            try:
                if s.startswith("file://"):
                    import urllib.request
                    path_str = urllib.request.url2pathname(s[len("file://"):])
                    if os.name == 'nt' and path_str.startswith('/') and path_str[2] == ':':
                        path_str = path_str[1:]
                    p = Path(path_str)
                else:
                    p = Path(s)
                if p.exists():
                    img = Image.open(p)
                    return img.size
            except Exception as e:
                logger.warning(f"[agnes] 读取本地图片尺寸失败: {e}")
                return None
        return None

    def _adjust_size_to_16_multiple(self, w: int, h: int) -> Tuple[int, int]:
        new_w = max(16, (w // 16) * 16)
        new_h = max(16, (h // 16) * 16)
        return new_w, new_h

    def _compute_aspect_ratio(self, w: int, h: int, candidate_ratios: List[str] = None) -> Optional[str]:
        """根据原图宽高，计算最贴近的预设长宽比"""
        if w <= 0 or h <= 0:
            return None
        ratios = candidate_ratios or list(PRESET_ASPECT_RATIOS)
        original_ratio = w / h
        best_ratio = None
        min_diff = float("inf")
        for r in ratios:
            try:
                rw, rh = map(int, r.split(":"))
                diff = abs(original_ratio - (rw / rh))
                if diff < min_diff:
                    min_diff = diff
                    best_ratio = r
            except Exception:
                continue
        return best_ratio
