# GIF 与 GPT Image 2.6

GPT Image 2.6 的官方模型 ID 包括 `gpt-image-2.6-flare` 和 `gpt-image-2.6-sunburst`。模型名直接透传给上游；中转渠道使用其实际公布的 ID，不需要插件另行添加白名单。

## 配置步骤

1. 在 Dashboard“绘图渠道”新建专用渠道，例如 ID `gif`，选择 `openai_image`，填写接口根地址、Key 和支持 GIF 的模型。
2. 将该渠道“输出格式”设为 `gif`，按需设置帧数、帧时长和复杂度。带参考图时确认已开启参考图支持。
3. 在预设页编辑内置 `GIF表情`（或自己的动图预设），将“指定渠道”选为该渠道，保存配置。
4. 附带人物参考图发送 `#GIF表情 轻轻眨眼`；纯文生图可发送 `#文生图 GIF表情 一只向镜头挥爪的小猫`。

预设绑定后不会回退到其他渠道，也不会被本会话的静态模型覆盖。渠道内的模型回退仍生效，因此专用渠道应只配置支持 GIF 的模型。安装或升级只新增缺失的 `GIF表情` 预设，不覆盖同名自定义提示词，也不会自动绑定渠道或消耗生成额度。

## 参数

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `image_output_format` | `auto` | `auto` 不发送格式参数；可选 `png`、`jpeg`、`webp`、`gif`。渠道的 `auto` 可覆盖全局指定格式。 |
| `image_frame_count` | `9` | 仅 GIF：2–16 帧。 |
| `image_frame_duration_ms` | `200` | 仅 GIF：每帧 20–2000 毫秒，必须为 10 的倍数。 |
| `image_complexity` | `auto` | `auto` 不发送复杂度参数；可选 `low`、`medium`、`high`，需模型支持。 |

全局和每条渠道均可设置这些字段。为了让普通静态绘图保持原行为，建议只在 GIF 专用渠道打开动画格式。

GIF 请求使用 Images API（文生图 `/v1/images/generations`、图生图 `/v1/images/edits`），发送 `output_format: "gif"`、`frame_count`、`frame_duration_ms`；JSON 和 multipart 编辑上传均支持。GIF 使用 `size: "auto"`，避免尺寸、帧数和张数的总像素预算冲突。GIF 不发送 `stream`、`partial_images` 或 `output_compression`。具体输出尺寸和时长仍由模型、渠道及用量限制决定。

## 发送和保存

插件按返回字节识别格式，完整保留 GIF 动画。QQ 发送副本不会将动图压成静态图；成功记录及下载保留原文件。Dashboard 的列表缩略图或大图预览可能是静态预览，原文件下载和聊天发送保持动画。PDF 以静态页面呈现图片，需要动画时请直接发送或下载 GIF。

如果上游返回静态图，请检查实际模型、渠道输出格式和服务商支持。提示词写“GIF”不能替代 `output_format`。如需排查真实生成，请通过工作台选择专用渠道手动测试，会消耗上游额度。

官方文档：

- [动画 GIF 参数](https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output)
- [复杂度](https://developers.openai.com/api/docs/guides/image-generation#set-image-complexity)
- [GPT Image 2.6 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.6-flare)
- [GPT Image 2.6 Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.6-sunburst)
