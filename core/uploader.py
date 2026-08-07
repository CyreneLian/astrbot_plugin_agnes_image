"""
Agnes AI 图像与视频上传、图床及文件服务魔改模块
"""
import os
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiohttp

from astrbot.api import logger
from astrbot.api.message_components import BaseMessageComponent
from astrbot.core.message.components import ComponentType

class DirectUrlImage(BaseMessageComponent):
    """直接发送 URL 图片的组件，绕过 AstrBot 转换直发"""
    type: ComponentType = ComponentType.Image
    file: str
    def __init__(self, url: str):
        super().__init__(file=url)

class Uploader:
    """处理公网图床上传、第三方图床上传以及 AstrBot 本地文件服务注册"""
    def __init__(self, plugin: Any):
        self.plugin = plugin

    def _find_url_in_json(self, data: Any) -> Optional[str]:
        """递归查找 JSON 响应中的 URL"""
        if isinstance(data, str):
            if data.startswith(("http://", "https://")):
                return data
            return None
        if isinstance(data, dict):
            for k, v in data.items():
                if k.lower() in ("url", "link", "href", "shorturl"):
                    if isinstance(v, str) and v.startswith(("http://", "https://")):
                        return v
                res = self._find_url_in_json(v)
                if res:
                    return res
        if isinstance(data, list):
            for item in data:
                res = self._find_url_in_json(item)
                if res:
                    return res
        return None

    async def upload_to_public_host(self, file_path: str) -> str:
        """上传到免费公网图床 (Telegraph / Catbox)"""
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

    async def upload_to_third_party(self, file_path: str, upload_url: str, token: str) -> str:
        """上传到第三方图床"""
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
