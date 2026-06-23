# Agnes 图像与视频生成插件

一个轻量、专注于 **Agnes AI** 图像与视频生成能力的 AstrBot 插件。依据 Agnes 官方文档进行了原生适配以实现完全免费、较高质量的定制化生成体验，支持**文生图**、**图生图（Img2Img）**以及**视频生成**。提供完整的中文使用说明、可选分辨率 / 长宽比 / 质量档位，并内置了支持 **URL 极致直发**与 **Auto 智能分流** 的多种发送模式，确保大体积媒体文件也能稳定送达。

## 功能特性

### 💎 完全免费与较高质量

- **完全免费**：前往官方主页 [agnes-ai.com](https://agnes-ai.com) 注册即可获取免费 API Key。
- **较高质量**：Agnes-Image-2.0-Flash 在 Artificial Analysis Image Editing 排行榜中位列 Top 20（ELO 1,184），2.1 版本更针对高信息密度与复杂视觉细节进行了深度优化。

### 🎨 图像与视频生成

- **文生图（Text-to-Image）**：根据自然语言描述直接生成图像
- **图生图（Image-to-Image）**：回复一张参考图后，附带描述对参考图进行修改；走 Agnes 原生 `extra_body.image` 通道（支持公网 URL 或 Data URI Base64）
- **视频生成（Video Generation）**：调用 `agnes-video-v2.0` 模型，支持纯文本生成视频，或带参考图生成视频（保留原图尺寸或按预设比例）。

### 🚀 多种智能发送模式

- **URL 极致直发模式**：将原始 URL 直接交给平台拉取，实现绝对的零带宽占用与极速发送。
- **Auto 防超时兜底模式**：智能拦截 4K 大图与长视频，自动下载并使用 NapCat Stream API 进行分块流式上传，彻底解决大文件发送导致的 WebSocket 超时卡死问题；小图则无缝切换为 Base64 inline 发送。

### 🎛 丰富的参数控制

- **生图分辨率档位**：`1K` / `2K` / `4K`（固定尺寸池下发，避免后端默默回退）
  - ⚠️ `4K` 仅支持 `agnes-image-2.1-flash`，且因文件较大不推荐
  - ⚠️ `agnes-image-2.0-flash` 仅支持 `1K` / `2K`
- **视频分辨率档位**：`480p` / `720p` / `1080p`
- **视频生成时长**：`3s` / `5s` / `10s` / `15s`
- **预设长宽比**：生图支持 10 种比例（如 `16:9`、`1:1`、`3:4` 等），视频支持 5 种比例（`16:9`、`9:16`、`1:1`、`4:3`、`3:4`）
- **4 档图像质量**：`auto`（不附加）/ `low` / `medium` / `high`（仅生图）
- **模型切换**：内联 `--model` 或 `模型2.1` 可临时切换生图模型

### ⚡ 稳定可靠

- **网络异常自动重试**：`ConnectionResetError` / `ClientPayloadError` / `ClientOSError` 最多自动重试 3 次（间隔 1 秒）
- **请求成功重置计数器**：避免误统计历史重试
- **配置可调超时**：`request_timeout` 默认 300 秒
- **视频异步生成与多字段兼容轮询**：针对视频接口（`agnes-video-v2.0`）采用异步任务制，插件在提交任务后，会优先使用标准端点 `/videos/{task_id}` 进行 5 秒间隔的异步状态查询。为了应对接口返回结构的多变性，插件深度兼容了 `url`、`video_url`、`mp4_url` 等多种完成字段以及嵌套结构，彻底解决了视频生成虽然完成但因字段不匹配而导致的超时报错。
- **三道防线缓存清理**：
  - 发送成功 / 失败后立即清理临时文件
  - `try...finally` 异常路径兜底
  - 启动时扫描清理超过 1 小时的残留文件

### 📊 统计展示

文生图成功后自动输出耗时统计行：

> ⏱ API响应 Xs | 发送 Ys | 重试 N次

### 🔌 平台适配与直发原理

为了兼顾发送速度、服务器带宽占用以及不同消息平台的兼容性，插件对不同平台进行了深度的差异化适配：

#### 1. QQ 平台（`aiocqhttp` / OneBot V11 协议端）
- **真正的 URL 极致直发**：当发送方式选择为 `url` 时，插件会使用自定义的 `DirectUrlImage` 组件，将原始 URL 封装在消息链中直接递给 OneBot 客户端（如 NapCat）。**AstrBot 本身不下载文件，完全由 QQ 协议端在后台拉取并发送**，实现服务器 0 带宽占用与极速发送。
- **Auto 模式防超时兜底**：为了解决 4K 图片或长视频直发导致的 WebSocket 超时卡死问题，当选择 `auto` 模式时，插件会自动将大文件下载到服务器，并采用 **NapCat Stream API** 分块流式上传，实现无感知的极速降级发送。
- **视频直发**：视频默认使用 `Video.fromURL` 发送，同样由协议端自己拉取。
- **参考图提取优化**：在进行“改图”或“图生视频”时，插件会**优先提取消息中原本的公网图片 URL** 直接传给 Agnes API，避免了“先下载到本地再转 base64 传输”的性能损耗，彻底解决了 base64 导致视频生成接口超时挂起的问题。

#### 2. 网页端（`webchat` / 4.26 OpenAPI）
- **标准组件兼容模式**：由于网页端底层不识别自定义直发组件，插件检测到 `webchat` 平台时，会自动将 `url` 直发模式降级为标准的 `AstrImage.fromURL` 模式。此时 AstrBot 会在后台将图片下载并缓存到本地，然后以 `[IMAGE]` 形式在网页端正常渲染。
- **视频链接辅助显示**：由于网页端不支持原生视频播放组件，在直发视频时，插件会在统计行上方自动附带**可点击的视频公网下载链接**，方便网页端用户直接点击观看。

#### 3. 其它平台（如微信、KOOK、Telegram 等）
- ⚠️ **未进行完整测试**。在这些平台下，插件会默认回退到标准组件兼容模式。若遇到图片或视频无法显示或发送时间过长的问题，建议将发送方式切换为 `auto`（智能切换）模式，通过本地文件缓存（`file://`）或 Base64 方式进行发送。


### 🧹 优雅发图策略

- **URL 极致直发（默认优先）**：由于 Agnes 官方 API 默认返回公网可访问的图片 URL，当配置为 `url` 模式时，插件会直接将原始 URL 封装在消息链中递给平台（如 NapCat）拉取，实现绝对的零带宽占用与极速发送。
- **Auto 智能分流与兜底**：当配置为 `auto` 模式时，插件会先将大体积图片/视频下载到本地，并优先使用 **NapCat Stream API** 进行分块流式上传，避免大文件普通发送导致的 WebSocket 超时卡死；若 Stream 不可用才退回普通文件发送；对于小图则直接使用 Base64 inline 发送。
- **Base64 分流直发（兼容兜底）**：若未来后端返回 `b64_json` 格式，则会自动激活分流策略：
  - **小图（<2MB）**：`aiocqhttp` 平台走 `base64://` inline 直发，减少文件 IO
  - **大图（≥2MB）**：写临时文件走 `file://` 本地路径，避免 Base64 编码膨胀 33% 导致的传输卡顿
- **多层级异常防线**：在 `cmd_generate` 各个阶段（参考图提取、配置构建、API 调用、图片发送）均增加了精细化的异常捕获与格式化输出，不再掩盖底层报错，让问题无处遁形。
- **避免自动 Reply 引用**：使用 `event.send` 直接发送，绕过 `result_decorate` pipeline，使发图消息保持干净，不携带原指令引用。

## 使用指令

### 基础指令

| 指令 | 说明 |
| --- | --- |
| `生图 <描述>` | 文生图 |
| `改图 <描述>` | 图生图或多图合成（需先回复一张或多张图片） |
| `生视频 <描述>` | 生成视频（回复图片时自动切换为图生视频） |
| `Agnes帮助` | 显示完整使用说明 |

### 快捷中文参数（直接连写在描述后即可）

```
生图 <描述> [尺寸2K] [比例16:9] [质量高] [模型2.1]
改图 <描述> [尺寸...] [比例...] [质量...] [模型...] [保留原比例]
生视频 <描述> [尺寸1080p] [比例16:9] [保留原比例]
```

| 中文前缀 | 对应英文 | 支持的值 |
| --- | --- | --- |
| `尺寸` | `--res` | `1K`/`2K`/`4K`（生图），`480p`/`720p`/`1080p`（视频） |
| `比例` | `--ratio` | `16:9`/`1:1`/`4:3` 等预设比例 |
| `质量` | `--quality` | `高`/`中`/`低`/`自动` |
| `模型` | `--model` | `2.1`/`2.0` |
| `保留原比例` | `--keep-size` | （无值标志）图生图/视频时保留参考图比例 |

> **提示**：插件完全向下兼容原有的 `--res`、`--ratio` 等英文参数格式。

### 示例

```
生图 一只猫
生图 赛博朋克城市夜景 尺寸4K 比例16:9 模型2.1
生图 一只猫 尺寸1K 比例16:9 质量高 模型2.0
改图 把它变成水彩画 比例1:1 质量高 保留原比例
生视频 樱花飘落 尺寸1080p 比例16:9
```

### 💡 图生视频参考图传输方式配置推荐与原理

图生图和图生视频的参考图输入规则并不完全相同：图生图走 Agnes 图像接口，插件可以将本地图片转换为 Data URI / Base64 后提交，因此通常可以正常读取本地图片；而图生视频走 Agnes-Video-V2.0 视频接口，官方文档要求图生视频、多图视频和关键帧动画使用可公网访问的图片 URL。

因此，下面的传输方式主要用于 **图生视频**：当用户在聊天软件（如 QQ）里发送本地图片时，插件需要先把这张参考图转换成 Agnes 视频接口能够访问的公网 URL。插件提供了三种转换方式，其原理与配置推荐如下：

1. **AstrBot 自带文件服务 (`astrbot`) —— 🌟 强烈推荐**
   - **原理**：直接利用 AstrBot 内置的文件服务将本地图片注册为一个临时下载链接（如 `http://你的IP:6185/api/file/xxx`），再把该链接交给 Agnes 视频接口读取。
   - **优点**：**速度极快**（本地直接生成链接，无需上传至外部服务器），**安全性极高**（图片数据完全保留在自己的服务器上，不经过任何第三方图床，避免隐私泄露）。
   - **缺点**：需要你的 AstrBot 面板支持公网访问，且需要正确配置 `video_file_service_base_url`（或全局的 `callback_api_base`）。
   - **⚠️ 注意**：选用此方式时，**务必开启**“是否开启 AstrBot 文件服务永久链接补丁”并**重启 AstrBot**，以防 Agnes 视频接口因排队多次拉取而报 404 错误。

2. **免费公网图床 (`free_public`)**
   - **原理**：插件自动将图片上传至 Telegraph 或 Catbox 免费图床，生成公网图片 URL 后交给 Agnes 视频接口读取。
   - **优点**：无需任何配置，开箱即用。
   - **缺点**：上传速度受国内连接海外图床网络影响，且图片会上传至公共图床中。

3. **自定义第三方图床 (`third_party`)**
   - **原理**：上传至你配置的私有或商业图床（如 ImgBB、SM.MS 等），生成公网图片 URL 后交给 Agnes 视频接口读取。
   - **优点**：适合有稳定图床资源的用户。
   - **缺点**：需要额外配置上传 API 地址和 Token，且第三方接口失效时会回退到免费图床。

> **注意**：分辨率档 `1K` / `2K` / `4K` 使用**固定尺寸池**下发；
> `4K` 仅支持 `agnes-image-2.1-flash`，`agnes-image-2.0-flash` 不支持。

### 💡 AstrBot 文件服务永久链接补丁说明

当本地图片传输方式选择 `astrbot`（使用 AstrBot 自带文件服务）时，插件需要将本地图片注册为 AstrBot 的临时下载链接提供给 Agnes 视频 API 进行下载。

**为什么要开启此补丁？**
AstrBot 底层自带的文件服务生成的 Token 是极其严格的 **一次性链接**。只要这个链接被任何程序访问了一次，它就会在毫秒内立刻销毁。而 Agnes 的视频生成 API 在接收到任务时，会先发起一次轻量级的预检请求来验证图片链接的有效性。这会导致一次性 Token 被提前消耗掉。随后，当视频生成的后台核心程序真正去下载图片时，由于链接已经失效，就会遭遇 404 报错。

开启 **“AstrBot 文件服务永久链接补丁”** 后，插件会使用 Monkey Patch 动态拦截并改写 AstrBot 的文件 Token 注册服务，将生成的文件链接有效期延长至 **5 分钟**（并非永久，但足够视频 API 完成下载），从而彻底解决 404 报错。

⚠️ **注意**：开启或关闭此补丁后，**必须重启整个 AstrBot 进程**以使补丁生效（因为热重载无法重置已被动态 Patch 写入 Python 内存的模块）。

## 适配细节

Agnes AI 没有实现标准的 OpenAI `/v1/images/edits` 端点。本插件依据 Agnes 官方文档（`Agnes Image 2.0/2.1 Flash`）做了原生适配：

- **端点**：`POST https://apihub.agnes-ai.com/v1/images/generations`
- **文生图**：仅需 `model` / `prompt` / `size`（Base64 模式下发送 `return_base64: true`）
- **图生图**：在 `extra_body.image` 数组中放参考图（支持公网 URL 或 Data URI Base64，Base64 模式下发送 `extra_body.response_format: "b64_json"`）
- **不**发送 `tags: ["img2img"]`（官方明确不需要）
- **自动解析** Agnes 特有的非标准 `usage` 嵌套结构
- **图片大小写归一**：自动剥除 `aiocqhttp` 平台的 `base64://` 前缀，统一转 Data URI

## 安装方式

### 方式一：通过 AstrBot 插件市场安装

在 AstrBot 管理面板中进入插件市场，搜索 `astrbot_plugin_agnes_image`，点击安装后重启 AstrBot。

### 方式二：手动安装

1. 下载或克隆本插件到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/CyreneLian/astrbot_plugin_agnes_image.git
```

2. 安装依赖（如 AstrBot 未自动安装）：

```bash
cd /AstrBot/data/plugins/astrbot_plugin_agnes_image
pip install -r requirements.txt
```

3. 重启 AstrBot。

4. 在插件设置中填写 `api_key`，并根据需要调整图片/视频发送方式、图生视频参考图传输方式等配置。

## 插件目录结构

```text
astrbot_plugin_agnes_image/
├── main.py              # 插件主逻辑：指令注册、图像/视频生成流程、发送策略与 AstrBot 文件服务补丁
├── agnes_api.py         # Agnes API 封装：生图、图生图、生视频任务提交与结果解析
├── napcat_stream.py     # NapCat Stream API 分块上传兜底发送逻辑
├── _conf_schema.json    # AstrBot 插件配置项定义
├── metadata.yaml        # 插件元数据：名称、版本、作者、描述等
├── requirements.txt     # Python 依赖声明
├── README.md            # 插件说明文档
├── logo.png             # 插件图标
└── .gitignore           # Git 忽略规则
```

## 配置说明

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `api_base` | Agnes AI 网关地址 | `https://apihub.agnes-ai.com/v1` |
| `api_key` | API 密钥（前往 agnes-ai.com 注册获取免费 Key） | （必填） |
| `model` | 生图模型 | `agnes-image-2.1-flash` |
| `proxy` | 代理地址（留空不使用，支持 http/https/socks5） | （空） |
| `default_resolution` | 默认分辨率档位（`1K`/`2K`/`4K`） | `2K` |
| `default_aspect_ratio` | 默认长宽比（10 种预设） | `1:1` |
| `default_quality` | 默认质量档（`auto`/`low`/`medium`/`high`） | `auto` |
| `output_format` | 图片发送方式（`url` 直发 / `auto` 智能切换） | `url` |
| `auto_threshold` | 智能切换文件大小阈值 (MB) | `2` |
| `keep_original_size` | 改图时按参考图原比例生图（自动匹配最接近的预设比例） | `false` |
| `request_timeout` | API 请求超时时间（秒） | `300` |
| `video_model` | 生视频模型 | `agnes-video-v2.0` |
| `video_default_resolution` | 默认视频分辨率（`480p`/`720p`/`1080p`） | `480p` |
| `video_default_aspect_ratio` | 默认视频长宽比 | `16:9` |
| `video_default_duration` | 默认视频时长（`3s`/`5s`/`10s`） | `5s` |
| `video_output_format` | 视频发送方式（`url` 直发 / `auto` 智能切换） | `url` |
| `video_keep_original_size` | 图生视频时是否保留参考图比例 | `true` |
| `video_request_timeout` | 视频生成及状态轮询的最大超时时间（秒） | `1000` |
| `video_img_handling_method` | 图生视频本地参考图传输方式（`astrbot`/`third_party`/`free_public`） | `free_public` |
| `video_enable_astrbot_file_magic` | 是否开启 AstrBot 文件服务永久链接补丁 | `true` |
| `video_file_service_base_url` | AstrBot 本地文件服务公网访问地址（面板外网地址，如 `http://IP:6185`） | （空） |
| `third_party_upload_url` | 第三方图床上传 API 地址 | （空） |
| `third_party_token` | 第三方图床上传 Token / Key | （空） |

## 关于 Agnes AI

- **官方主页**：[agnes-ai.com](https://agnes-ai.com/)
- **API 控制台**：[apihub.agnes-ai.com](https://apihub.agnes-ai.com/)
- **计费**：目前模型调用全部免费

## 系统要求

- AstrBot `>=4.16`
- Python 3.12+

## 联系方式

- QQ: 1158026885

## 作者

- 往昔的涟漪

## 如果这个插件对你有帮助，欢迎给个 ⭐ Star！
