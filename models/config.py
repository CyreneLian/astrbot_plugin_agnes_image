"""
Agnes 图像与视频生成插件配置模型
"""
from dataclasses import dataclass, field
from typing import Dict, Any

@dataclass
class AgnesPluginConfig:
    """Agnes 插件配置模型"""
    api_key: str = ""
    api_base: str = "https://api.agnes-ai.cn/v1"
    proxy: str = ""
    enable_llm_tools: bool = True
    model: str = "agnes-image-2.5-flash"
    default_resolution: str = "1K"
    default_aspect_ratio: str = "3:2"
    default_quality: str = "high"
    output_format: str = "url"
    auto_threshold: int = 2
    keep_original_size: bool = True
    request_timeout: int = 300

    # 视频相关配置
    video_model: str = "agnes-video-2.5-flash"
    video_default_resolution: str = "720p"
    video_default_aspect_ratio: str = "16:9"
    video_default_duration: str = "12s"
    video_output_format: str = "url"
    video_keep_original_size: bool = True
    video_request_timeout: int = 1000
    video_auto_threshold: int = 2
    video_img_handling_method: str = "astrbot"
    video_enable_astrbot_file_magic: bool = True
    video_file_service_base_url: str = ""
    third_party_upload_url: str = ""
    third_party_token: str = ""
    third_party_file_field: str = "image"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        """安全整数解析：非法字符 / 负数 / 空值一律回退默认值，绝不抛异常（兼容小数截断）"""
        try:
            v = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "AgnesPluginConfig":
        """从字典创建配置实例"""
        return cls(
            api_key=str(config.get("api_key", "")).strip(),
            api_base=str(config.get("api_base", "https://api.agnes-ai.cn/v1")).strip(),
            proxy=str(config.get("proxy", "")).strip(),
            enable_llm_tools=bool(config.get("enable_llm_tools", True)),
            model=str(config.get("model", "agnes-image-2.5-flash")).strip(),
            default_resolution=str(config.get("default_resolution", "1K")).strip(),
            default_aspect_ratio=str(config.get("default_aspect_ratio", "3:2")).strip(),
            default_quality=str(config.get("default_quality", "high")).strip(),
            output_format=str(config.get("output_format", "url")).strip(),
            auto_threshold=cls._safe_int(config.get("auto_threshold"), 2),
            keep_original_size=bool(config.get("keep_original_size", True)),
            request_timeout=cls._safe_int(config.get("request_timeout"), 300),

            video_model=str(config.get("video_model", "agnes-video-2.5-flash")).strip(),
            video_default_resolution=str(config.get("video_default_resolution", "720p")).strip(),
            video_default_aspect_ratio=str(config.get("video_default_aspect_ratio", "16:9")).strip(),
            video_default_duration=str(config.get("video_default_duration", "12s")).strip(),
            video_output_format=str(config.get("video_output_format", "url")).strip(),
            video_keep_original_size=bool(config.get("video_keep_original_size", True)),
            video_request_timeout=cls._safe_int(config.get("video_request_timeout"), 1000),
            video_auto_threshold=cls._safe_int(config.get("video_auto_threshold"), 2),
            video_img_handling_method=str(config.get("video_img_handling_method", "astrbot")).strip(),
            video_enable_astrbot_file_magic=bool(config.get("video_enable_astrbot_file_magic", True)),
            video_file_service_base_url=str(config.get("video_file_service_base_url", "")).strip(),
            third_party_upload_url=str(config.get("third_party_upload_url", "")).strip(),
            third_party_token=str(config.get("third_party_token", "")).strip(),
            third_party_file_field=str(config.get("third_party_file_field", "image")).strip() or "image",
        )

    def validate(self) -> bool:
        """验证配置有效性"""
        if self.auto_threshold <= 0:
            raise ValueError("智能切换文件大小阈值必须大于 0 MB")
        if self.request_timeout <= 0:
            raise ValueError("图片 API 请求超时时间必须大于 0 秒")
        if self.video_request_timeout <= 0:
            raise ValueError("视频生成超时时间必须大于 0 秒")
        if self.video_auto_threshold <= 0:
            raise ValueError("视频智能切换文件大小阈值必须大于 0 MB")
        return True
