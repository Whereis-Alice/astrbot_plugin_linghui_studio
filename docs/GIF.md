# GIF 预设

GIF 是预设的请求参数，不需要为已有服务复制一个渠道。同一个渠道可以处理普通静图和 GIF；每次请求的参数独立，并发请求不会互相改变格式。

## 配置

1. 在绘图渠道填写服务商地址、密钥、实际模型名和支持的默认分辨率。确认服务商支持 GIF；不要仅凭模型名称推断。
2. 编辑 `GIF表情` 或自己的 `动图画` 预设，将“输出格式”设为 **GIF**，填写帧数、每帧时长和可选复杂度。
3. 在该预设选择一个或多个支持 GIF 的现有渠道，通过上移／下移确定主用、备用顺序。例如 `miku → orb`。保存后失败仅在所选渠道之间回退，不会使用未选渠道。
4. 附图发送 `#动图画`，或无图发送 `#文生图 GIF表情 一只眨眼的小猫`。

内置 `GIF表情` 默认请求 GIF。其他旧预设默认跟随渠道设置，不根据提示词中的“GIF”自动猜测；需要动画时显式选择 GIF。安装或升级不会自动调用生成接口。

如果服务商有尺寸限制，在渠道上设置其分辨率。例如只支持 1K 的渠道设为 1K。GIF 的实际请求使用 `size: "auto"`，以适配多帧总像素限制；静图仍使用原有尺寸设置。

## 参数与优先级

| 预设参数 | 默认 | 说明 |
| --- | --- | --- |
| `image_output_format` | `auto` | 跟随渠道；可显式选择 `png`、`jpeg`、`webp`、`gif`。 |
| `image_frame_count` | `9` | GIF：2–16 帧。 |
| `image_frame_duration_ms` | `200` | GIF：每帧 20–2000 毫秒，为 10 的倍数。12 帧 × 170ms 约为 2.04 秒。 |
| `image_complexity` | `auto` | `low`、`medium`、`high` 或 `auto`，以渠道支持为准。 |

渠道选择和图片参数统一保存在 `preset_generation_options`，原生配置只显示一个“预设设置”入口；其中 `channel_ids` 为主用到备用的有序列表。旧版分离保存的绑定会自动迁移，旧导出仍可导入。预设的显式输出格式优先于渠道默认，`auto` 完整跟随渠道参数。预设多渠道顺序优先于会话、全局路由；会话模型选择不会覆盖已绑定预设的渠道模型。每个渠道自身的模型回退仍然生效。

模型名原样透传，没有只允许 2.6 的白名单。OpenAI 官方文档明确列出 GPT Image 2.6 Sunburst / Flare 的 GIF 能力；中转渠道可能使用其他名称或能力映射，例如已实测支持 GIF 的 `gpt-image-2.5-sunburst`，按服务商实际结果配置即可。

## 请求、验证与发送

GIF 使用 Images API：文生图 `/v1/images/generations`，图生图 `/v1/images/edits`。JSON、multipart 均传 `output_format: "gif"`、`frame_count`、`frame_duration_ms`，不传 `stream`、`partial_images`、`output_compression`。接口模式 `auto` 会为明确的 GIF 请求选择 Images 接口。

GIF 请求若返回 PNG 或单帧 GIF，会作为失败处理并按所选渠道和现有回退策略处理，不会冒充动画发送。内容审核拒绝遵循原有内容审核回退配置。成功后按原始字节缓存并发送到 QQ，完整保留动画；缩略图、PDF 为静态展示，下载保留原文件。

官方文档：[动画 GIF 参数](https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output)。
