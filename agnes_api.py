"""
Agnes AI 图像生成核心模块

封装 Agnes AI 官方 API（兼容 Agnes Image 2.0/2.1 Flash）：
- 端点：POST https://apihub.agnes-ai.com/v1/images/generations
- 文生图：仅需 model/prompt/size
- 图生图：在请求体顶层加 image 数组（支持 URL 或 Data URI Base64）
- 官方文档明确：不要把 image 嵌套在 extra_body 里、不要把 response_format 放在顶层、不要发 tags: ["img2img"]
- 特殊 usage 解析：Agnes 把 usage 嵌套在 data 数组对象里
"""

from __future__ import annotations

import asyncio
import logging
import time
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class AgnesAPIError(Exception):
    """Agnes API 调用错误，携带 HTTP 状态、响应 body、错误码等上下文。

    Attributes:
        status: HTTP 状态码（如 400/401/429/500），None 表示网络层错误
        body: 原始响应 body（可能包含 JSON 中的 error 字段）
        error_code: 从 JSON 响应中提取的错误码（如 "quota_exceeded"）
        error_message: 从 JSON 响应中提取的错误信息（如 "今日调用次数已用完"）
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        api_latency: float | None = None,
        retries: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.error_code = error_code
        self.error_message = error_message
        self.api_latency = api_latency
        self.retries = retries


def _parse_error_body(body: str) -> tuple[str | None, str | None]:
    """尝试从响应 body 中提取 (error_code, error_message)。

    兼容多种常见结构：
    - {"error": {"code": "...", "message": "..."}}
    - {"error": {"message": "..."}}
    - {"error": "..."}（纯字符串）
    - {"code": "...", "message": "..."}
    - {"detail": "..."}
    """
    if not body:
        return None, None
    try:
        import json as _json

        data = _json.loads(body)
    except Exception:
        return None, None

    if not isinstance(data, dict):
        return None, None

    # 优先级 1: error 是 dict（含 code/message）
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        msg = err.get("message") or err.get("msg")
        if isinstance(code, str):
            code = code
        elif code is not None:
            code = str(code)
        if isinstance(msg, str):
            msg = msg
        elif msg is not None:
            msg = str(msg)
        return code, msg

    # 优先级 2: error 是字符串
    if isinstance(err, str):
        return None, err

    # 优先级 3: 顶层 code/message
    code = data.get("code") or data.get("error_code")
    msg = data.get("message") or data.get("msg") or data.get("detail")
    if isinstance(code, (int, float)):
        code = str(code)
    if isinstance(msg, (int, float)):
        msg = str(msg)
    return code, msg


# ============== 预设配置 ==============

# 分辨率档位（使用固定尺寸池；4K 仅 agnes-image-2.1-flash 支持）
PRESET_RESOLUTIONS = ("1K", "2K", "4K")

# 长宽比预设
PRESET_ASPECT_RATIOS = (
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "9:16",
    "4:5",
    "5:4",
    "21:9",
    "3:4",
    "2:3",
)

# 质量档（作为 suffix 追加到 prompt）
PRESET_QUALITIES = ("auto", "low", "medium", "high")

# 固定尺寸池：Agnes 对非标准 WxH 可能回退到 1K，因此不要动态推导任意尺寸。
# 1K 档尽量使用常见尺寸；2K 档使用对应 2 倍或主流长边尺寸。
SIZE_PRESETS: dict[str, dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "4:3": "1024x768",
        "3:2": "1008x672",
        "9:16": "720x1280",
        "4:5": "832x1040",
        "5:4": "1040x832",
        "21:9": "1344x576",
        "3:4": "768x1024",
        "2:3": "672x1008",
    },
    "2K": {
        "1:1": "2048x2048",
        "16:9": "2048x1152",
        "4:3": "2048x1536",
        "3:2": "2016x1344",
        "9:16": "1152x2048",
        "4:5": "1664x2080",
        "5:4": "2080x1664",
        "21:9": "2016x864",
        "3:4": "1536x2048",
        "2:3": "1344x2016",
    },
    "4K": {
        "1:1": "2880x2880",
        "16:9": "3840x2160",
        "4:3": "3264x2448",
        "3:2": "3504x2336",
        "9:16": "2160x3840",
        "4:5": "2560x3200",
        "5:4": "3200x2560",
        "21:9": "3696x1584",
        "3:4": "2448x3264",
        "2:3": "2336x3504",
    },
}


# ============== 数据类 ==============


@dataclass
class AgnesRequestConfig:
    """Agnes 生图请求配置"""

    api_base: str
    api_key: str
    model: str
    prompt: str
    resolution: str = "2K"
    aspect_ratio: str = "1:1"
    reference_images: list[str] = field(default_factory=list)
    proxy: Optional[str] = None
    custom_size: Optional[str] = None  # 直接指定 WxH，覆盖 resolution+aspect_ratio
    timeout: int = 300  # API 请求超时时间
    output_format: str = "url"  # 输出格式：url 或 base64


# ============== 工具函数 ==============


def _is_agnes_ai(api_base: str, model: str) -> bool:
    """检测是否为 Agnes AI 服务"""
    base_lower = (api_base or "").lower()
    model_lower = (model or "").lower()
    return "agnes-ai" in base_lower or model_lower.startswith("agnes-image")


def _to_data_uri(image_input: str) -> str:
    """将输入规范化为 API 接受的格式。

    规则：
    - 空字符串 → 空字符串
    - 已经是 Data URI → 原样返回
    - HTTP(S) 公网 URL → 原样返回（API 支持）
    - 其他（裸 base64）→ 补上 image/png 的 Data URI 前缀
    """
    s = (image_input or "").strip()
    if not s:
        return ""
    if s.startswith("data:"):
        return s
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"data:image/png;base64,{s}"


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _resolve_size(resolution: str, aspect_ratio: str) -> str:
    """根据分辨率档与长宽比返回固定 WxH 尺寸。"""
    if resolution not in SIZE_PRESETS:
        resolution = "2K"
    if aspect_ratio not in PRESET_ASPECT_RATIOS:
        aspect_ratio = "1:1"
    return SIZE_PRESETS[resolution].get(aspect_ratio, SIZE_PRESETS[resolution]["1:1"])


def _build_agnes_payload(config: AgnesRequestConfig) -> dict[str, Any]:
    """构建 Agnes AI 请求体（图生图与文生图共用 /v1/images/generations）"""
    payload: dict[str, Any] = {
        "model": config.model,
        "prompt": config.prompt,
    }

    # custom_size 优先（如改图 --keep-size 时强制覆盖）
    if config.custom_size:
        payload["size"] = config.custom_size
    else:
        size_value = _resolve_size(config.resolution, config.aspect_ratio)
        if size_value:
            payload["size"] = size_value

    ref_images = config.reference_images or []
    is_img2img = False
    if ref_images:
        image_list = []
        for ref in ref_images:
            data_uri = _to_data_uri(ref)
            if data_uri:
                image_list.append(data_uri)
        if image_list:
            # 原插件（astrbot_plugin_gemini_image_generation）一直使用
            # extra_body.image 作为 Agnes AI 图生图通道
            payload["extra_body"] = {"image": image_list}
            is_img2img = True

    # 处理输出格式
    if config.output_format == "base64":
        if is_img2img:
            if "extra_body" not in payload:
                payload["extra_body"] = {}
            payload["extra_body"]["response_format"] = "b64_json"
        else:
            payload["return_base64"] = True

    return payload


def _extract_usage(usage: Any) -> dict[str, int]:
    """提取 token 使用量，兼容 Agnes 嵌套在 data 数组中的特殊格式"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0)

    # 兜底：Agnes 可能把 usage 嵌套在 data 数组对象里
    if total_tokens == 0 and prompt_tokens == 0:
        data = usage.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                if "usage" in item and isinstance(item["usage"], dict):
                    nested = item["usage"]
                    prompt_tokens += int(nested.get("prompt_tokens", 0) or 0)
                    completion_tokens += int(nested.get("completion_tokens", 0) or 0)
                    total_tokens += int(nested.get("total_tokens", 0) or 0)

    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _extract_image_b64(result: dict[str, Any]) -> Optional[str]:
    """从响应中提取图像 base64 / url"""
    data = result.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if first.get("b64_json"):
                return first["b64_json"]
            if first.get("url"):
                return first["url"]

    if result.get("b64_json"):
        return result["b64_json"]
    if result.get("image"):
        return result["image"]
    return None


# ============== 入口函数 ==============


# 模块级长连接 session 缓存：{proxy_key: ClientSession}
_agnes_session_cache: dict[str, aiohttp.ClientSession] = {}
_agnes_session_lock = asyncio.Lock()


async def _get_agnes_session(proxy: str | None) -> aiohttp.ClientSession:
    """获取（或创建）可复用的长连接 aiohttp ClientSession。

    优点：
    - 避免每次请求都做 TCP 握手 / TLS 握手（gemini 范式）
    - 启用 tcp_keepalive，避免被中间设备当死连接 RST
    - trust_env=False，避免被系统代理劫持
    """
    proxy_key = proxy or ""
    sess = _agnes_session_cache.get(proxy_key)
    if sess is not None and not sess.closed:
        return sess

    async with _agnes_session_lock:
        sess = _agnes_session_cache.get(proxy_key)
        if sess is not None and not sess.closed:
            return sess

        timeout = aiohttp.ClientTimeout(total=300)
        # SOCKS 代理需要 aiohttp-socks；HTTP/HTTPS 代理走 trust_env=False + proxy 参数
        if proxy and proxy.lower().startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy)
                sess = aiohttp.ClientSession(
                    timeout=timeout,
                    trust_env=False,
                    connector=connector,
                )
            except ImportError:
                logger.error("[agnes] SOCKS 代理需要安装 aiohttp-socks，将回退到无代理")
                connector = aiohttp.TCPConnector(keepalive_timeout=75, force_close=False)
                sess = aiohttp.ClientSession(
                    timeout=timeout,
                    trust_env=False,
                    connector=connector,
                )
        else:
            connector = aiohttp.TCPConnector(
                keepalive_timeout=75,
                force_close=False,
                enable_cleanup_closed=True,
            )
            sess = aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
                connector=connector,
            )

        _agnes_session_cache[proxy_key] = sess
        logger.debug(f"[agnes] 已创建长连接 session，proxy={proxy_key!r}")
        return sess


async def _close_agnes_sessions() -> None:
    """关闭所有缓存的 session（插件终止时调用，可选）"""
    async with _agnes_session_lock:
        for sess in _agnes_session_cache.values():
            if not sess.closed:
                await sess.close()
        _agnes_session_cache.clear()


async def generate_image(config: AgnesRequestConfig) -> dict[str, Any]:
    """
    调用 Agnes AI 生成图像（支持连接重置自动重试）

    返回：
    {
        "b64_json": Optional[str],  # 图像 base64
        "url": Optional[str],       # 图像 url
        "size": str,                # 实际尺寸，如 "2048x1152"
        "usage": {...},             # token 用量
        "raw": {...},               # 原始响应
        "api_latency": float,       # API 响应耗时（秒）
        "retries": int,             # 重试次数
    }
    """
    if not _is_agnes_ai(config.api_base, config.model):
        logger.warning(
            f"api_base={config.api_base} 或 model={config.model} 不像是 Agnes AI，"
            "请确认配置正确。"
        )

    payload = _build_agnes_payload(config)
    endpoint = config.api_base.rstrip("/") + "/images/generations"

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    logger.debug(
        f"[agnes] request: model={config.model} "
        f"size={payload.get('size')} "
        f"ref_images={len(payload.get('image', []))} "
        f"prompt_len={len(config.prompt)}"
    )

    max_retries = 5
    retries = 0
    api_start = time.monotonic()
    last_exc: Exception | None = None
    result: dict[str, Any] | None = None

    while retries < max_retries:
        try:
            # 长连接复用：keepalive + trust_env=False（避免被系统代理劫持）
            session = await _get_agnes_session(config.proxy)
            timeout_obj = aiohttp.ClientTimeout(total=config.timeout)
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout_obj,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    err_code, err_msg = _parse_error_body(text)
                    raise AgnesAPIError(
                        f"Agnes API 请求失败 status={resp.status}",
                        status=resp.status,
                        body=text,
                        error_code=err_code,
                        error_message=err_msg,
                        api_latency=round(time.monotonic() - api_start, 2),
                        retries=retries,
                    )
                try:
                    result = await resp.json()
                except Exception as e:
                    raise AgnesAPIError(
                        f"Agnes API 返回非 JSON: {e}",
                        status=resp.status,
                        body=text,
                        api_latency=round(time.monotonic() - api_start, 2),
                        retries=retries,
                    )
            # 请求成功，跳出重试循环
            break
        except (aiohttp.ClientPayloadError, aiohttp.ClientOSError, ConnectionResetError, asyncio.TimeoutError) as e:
            retries += 1
            last_exc = e
            # 指数退避：1s → 2s → 4s → 8s
            backoff = min(2 ** (retries - 1), 8)
            logger.warning(
                f"[agnes] API 请求遇到网络抖动/重置 ({type(e).__name__}: {e})，"
                f"正在进行第 {retries}/{max_retries} 次重试（退避 {backoff}s）..."
            )
            if retries >= max_retries:
                raise AgnesAPIError(
                    f"Agnes API 请求失败，重试 {max_retries} 次后仍被重置: {e}",
                    status=500,
                    body=str(e),
                    api_latency=round(time.monotonic() - api_start, 2),
                    retries=retries,
                )
            await asyncio.sleep(backoff)
        except Exception as e:
            # 其他非网络重置类异常，不进行重试，直接抛出
            raise e

    if result is None:
        # 兜底：理论上不会走到这里
        raise AgnesAPIError(
            f"Agnes API 请求异常，未拿到响应: {last_exc}",
            status=500,
            body=str(last_exc) if last_exc else "",
            api_latency=round(time.monotonic() - api_start, 2),
            retries=retries,
        )

    api_latency = time.monotonic() - api_start

    b64_or_url = _extract_image_b64(result)
    usage = _extract_usage(result.get("usage"))

    return {
        "b64_json": b64_or_url if b64_or_url and not b64_or_url.startswith("http") else None,
        "url": b64_or_url if b64_or_url and b64_or_url.startswith("http") else None,
        "size": payload.get("size"),
        "usage": usage,
        "raw": result,
        "api_latency": round(api_latency, 2),
        "retries": retries,
    }

# ==========================================
# 视频生成功能 (Agnes-Video-V2.0)
# ==========================================

@dataclass
class AgnesVideoRequestConfig:
    api_base: str
    api_key: str
    model: str
    prompt: str
    reference_images: list[str] = field(default_factory=list)
    duration: str = "5s"  # "3s", "5s", "10s"
    proxy: Optional[str] = None
    timeout: int = 300  # API 请求超时时间
    output_format: str = "url"  # "url" 或 "file"
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    width: Optional[int] = None
    height: Optional[int] = None

def _resolve_video_params(duration: str) -> dict[str, int]:
    # 根据官方文档，frame_rate 推荐 24
    if duration == "3s":
        return {"num_frames": 81, "frame_rate": 24}
    elif duration == "10s":
        return {"num_frames": 241, "frame_rate": 24}
    else:  # 默认 5s
        return {"num_frames": 121, "frame_rate": 24}

VIDEO_SIZE_PRESETS = {
    "480p": {
        "16:9": (854, 480),
        "9:16": (480, 854),
        "1:1": (480, 480),
        "4:3": (640, 480),
        "3:4": (480, 640),
    },
    "720p": {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "1:1": (720, 720),
        "4:3": (960, 720),
        "3:4": (720, 960),
    },
    "1080p": {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:3": (1440, 1080),
        "3:4": (1080, 1440),
    }
}

def _build_video_payload(config: AgnesVideoRequestConfig) -> dict[str, Any]:
    if config.width and config.height:
        w, h = config.width, config.height
    else:
        res = config.resolution or "720p"
        ratio = config.aspect_ratio or "16:9"
        
        # 映射尺寸，若匹配不到则回退到 1152x768
        size_map = VIDEO_SIZE_PRESETS.get(res, VIDEO_SIZE_PRESETS["720p"])
        w, h = size_map.get(ratio, (1152, 768))
        
        # 图生视频保留原尺寸逻辑
        ref_images = config.reference_images or []
        if ref_images and config.resolution == "keep":
            try:
                # 尝试从第一张参考图读取真实尺寸
                data_uri = _to_data_uri(ref_images[0])
                if data_uri:
                    import base64
                    import io
                    from PIL import Image
                    
                    if data_uri.startswith("data:"):
                        b64_data = data_uri.split(",", 1)[1]
                        raw_bytes = base64.b64decode(b64_data)
                        img = Image.open(io.BytesIO(raw_bytes))
                        w, h = img.size
                    elif data_uri.startswith("http://") or data_uri.startswith("https://"):
                        # 如果是 URL，同步下载图片读取尺寸
                        import urllib.request
                        # 使用超时限制防止同步请求卡死
                        with urllib.request.urlopen(data_uri, timeout=5) as response:
                            raw_bytes = response.read()
                        img = Image.open(io.BytesIO(raw_bytes))
                        w, h = img.size
                    
                    # 为了符合视频生成的常见要求，将宽高调整为16的倍数
                    def adjust_to_16(val: int) -> int:
                        return max(16, round(val / 16) * 16)
                    
                    w = adjust_to_16(w)
                    h = adjust_to_16(h)
                    logger.info(f"[agnes] 图生视频保留原尺寸已启用，参考图尺寸: {img.size} -> 调整为: {w}x{h}")
            except Exception as e:
                logger.warning(f"[agnes] 图生视频读取参考图尺寸失败: {e}，回退到默认尺寸")

            
    payload: dict[str, Any] = {
        "model": config.model,
        "prompt": config.prompt,
        "width": w,
        "height": h,
    }
    
    # 填入帧数和帧率
    params = _resolve_video_params(config.duration)
    payload.update(params)
    
    ref_images = config.reference_images or []
    if ref_images:
        image_list = []
        for ref in ref_images:
            data_uri = _to_data_uri(ref)
            if data_uri:
                image_list.append(data_uri)
        
        if len(image_list) == 1:
            # 单图生视频
            payload["image"] = image_list[0]
        elif len(image_list) > 1:
            # 多图视频或关键帧
            payload["extra_body"] = {"image": image_list}
            
    return payload

async def generate_video_task(config: AgnesVideoRequestConfig) -> tuple[str, float]:
    """
    提交视频生成任务，并轮询结果。返回最终的视频 URL。
    """
    payload = _build_video_payload(config)
    endpoint = config.api_base.rstrip("/") + "/videos"
    
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json"
    }
    
    start_time = time.time()
    # 1. 提交任务
    video_id = None
    task_id = None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                endpoint,
                headers=headers,
                json=payload,
                proxy=config.proxy,
                timeout=aiohttp.ClientTimeout(total=config.timeout)
            ) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    raise AgnesAPIError(f"提交视频任务失败 (HTTP {resp.status})", status=resp.status, body=resp_text)
                
                data = json.loads(resp_text)
                task_id = data.get("id") or data.get("task_id")
                video_id = data.get("video_id")
                if not task_id and not video_id:
                    raise Exception(f"提交视频任务失败，未返回任务 ID: {resp_text}")
        except Exception as e:
            raise Exception(f"提交视频任务请求失败: {e}")
            
    # 2. 轮询结果
    # 优先使用 /videos/{task_id} 标准轮询接口。旧的 /agnesapi?video_id=... 容易返回非最终状态或字段不稳定，
    # 会导致任务明明完成却一直等到超时。
    poll_id = task_id or video_id
    poll_endpoint = config.api_base.rstrip("/") + f"/videos/{poll_id}"
        
    max_wait = config.timeout
    last_status = "unknown"
    last_progress: Any = None
    last_error: Any = None
    last_resp_preview = ""
    
    def _pick_video_url(data: dict[str, Any]) -> str | None:
        candidates = [
            data.get("url"),
            data.get("video_url"),
            data.get("output_url"),
            data.get("download_url"),
            data.get("file_url"),
            data.get("mp4_url"),
        ]
        output = data.get("output") or data.get("result") or data.get("data")
        if isinstance(output, dict):
            candidates.extend([
                output.get("url"),
                output.get("video_url"),
                output.get("output_url"),
                output.get("download_url"),
                output.get("file_url"),
                output.get("mp4_url"),
            ])
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    candidates.extend([
                        item.get("url"),
                        item.get("video_url"),
                        item.get("output_url"),
                        item.get("download_url"),
                        item.get("file_url"),
                        item.get("mp4_url"),
                    ])
                elif isinstance(item, str):
                    candidates.append(item)
        for value in candidates:
            if isinstance(value, str) and value.startswith("http"):
                return value
                
        # 兜底：如果上面的字段都没命中，直接在整个 JSON 里找
        raw_str = json.dumps(data)
        import re
        # 简单粗暴的正则，匹配 http 开头直到双引号或结束
        matches = re.findall(r'https?://[^"]+', raw_str)
        for m in matches:
            if ".mp4" in m or "video" in m:
                # 过滤掉一些明显不是真实视频地址的 API 接口
                if "api" not in m and "webhook" not in m:
                    return m
        return None
    
    async with aiohttp.ClientSession() as session:
        while time.time() - start_time < max_wait:
            await asyncio.sleep(5)  # 官方推荐 5s 轮询间隔
            try:
                async with session.get(
                    poll_endpoint,
                    headers=headers,
                    proxy=config.proxy,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    resp_text = await resp.text()
                    if resp.status != 200:
                        logger.warning(f"[agnes] 轮询视频状态 HTTP {resp.status}: {resp_text}")
                        continue
                        
                    data = json.loads(resp_text)
                    status = (data.get("status") or data.get("state") or "").lower()
                    last_status = status or "unknown"
                    last_progress = data.get("progress")
                    last_error = data.get("error") or data.get("message") or data.get("detail")
                    last_resp_preview = resp_text[:500]
                    video_url = _pick_video_url(data)
                    
                    # 只要拿到了合法的 video_url，不管 status 是什么（甚至 failed），都视为成功。
                    # 因为部分 API 平台在生成完毕后，可能会因为回调或转码等周边服务报错，把最终状态写为 failed，
                    # 但其实视频核心文件已经生成并返回了。
                    if video_url:
                        return video_url, time.time() - start_time
                        
                    if status in ("completed", "complete", "succeeded", "success", "finished", "done"):
                        raise Exception(f"视频已完成，但未找到视频 URL: {resp_text[:300]}")
                    elif status in ("failed", "error", "cancelled", "canceled"):
                        err = data.get("error") or data.get("message") or data.get("detail") or "Unknown error"
                        raise Exception(f"视频生成失败: {err}")
                    # queued / pending / in_progress / processing / running 继续等待
            except AgnesAPIError:
                raise
            except Exception as e:
                # 轮询时的网络异常可以忽略，继续下一次轮询；完成但字段异常会带着响应预览抛出并记录
                logger.warning(f"[agnes] 轮询视频状态异常: {e}")
                
    raise Exception(
        "视频轮询超时：任务已提交且可能仍在 Agnes 后台排队/生成，"
        "不代表一定生成失败。"
        f"等待 {max_wait} 秒仍未拿到最终视频链接；"
        f"task_id={task_id or 'N/A'}；video_id={video_id or 'N/A'}；"
        f"最后状态={last_status}；进度={last_progress if last_progress is not None else 'N/A'}；"
        f"最后错误={last_error or 'N/A'}；最后轮询接口={poll_endpoint}；"
        f"最后响应预览={last_resp_preview}"
    )
