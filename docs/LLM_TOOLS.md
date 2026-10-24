# LLM 工具说明

灵绘工坊会向 AstrBot 注册以下工具。工具名称是给模型识别的内部名称，用户不需要手动输入；用户只要自然表达需求，模型会根据工具描述选择合适的工具。

所有图片工具都会经过相同的权限、额度、渠道回退、缓存记录和错误处理流程。工具调用不会改变手动指令的冷却计时。

## 工具一览

| 工具名称 | 作用 | 典型场景 |
| --- | --- | --- |
| `linghui_check_user_status` | 查询当前用户是否属于高权限/VIP身份，并返回人格语气提示。 | 纯聊天、需要判断对用户态度时。 |
| `linghui_draw_image` | 根据文字生成图片（文生图）。 | “画一只猫”“做一张海报”。 |
| `linghui_edit_image` | 编辑用户发送、引用或最近上下文中的图片（图生图）。 | “把这张图改成水彩风”“继续修改刚才那张”。 |
| `linghui_smart_generate` | 自动判断当前请求应走文生图还是图生图。 | 用户只说“帮我做成赛博朋克风”，但图片来源需要从上下文判断。 |
| `linghui_persona_photo` | 使用 Bot 人设参考图生成自拍、日常照、写真或合影。 | “发一张你的自拍”“换成白色连衣裙”。 |
| `linghui_batch_process` | 批量处理上下文中的多张图片，可选择打包 PDF。 | “把这几张都手办化”。 |
| `linghui_batch_process_concurrent` | 并发批量处理多张图片，速度优先。 | “快速处理这组图片”。 |
| `linghui_pack_images_to_pdf` | 把已有图片直接打包为 PDF，不会重新生成或修改图片。 | “把刚才的图片合成 PDF”。 |

## 怎么选择工具

| 用户意图 | 应选择 |
| --- | --- |
| 画新的角色、物体或场景，没有要参考的图片 | `linghui_draw_image` |
| 修改、转风格、扩图或继续修改已有图片 | `linghui_edit_image` |
| 无法确定是文生图还是图生图，但上下文中可能有图片 | `linghui_smart_generate` |
| 要看 Bot/人设本人的自拍、穿搭、写真或合影 | `linghui_persona_photo` |
| 一次处理多张图片 | `linghui_batch_process` 或 `linghui_batch_process_concurrent` |
| 只把已有图片整理成 PDF | `linghui_pack_images_to_pdf` |

用户不需要在聊天中输入这些工具名；它们是模型调用接口。调用前不要先输出“稍等”，插件会按配置发送自然的进度提示。

### `linghui_check_user_status`

无参数。仅用于文字聊天或需要决定回复风格时。用户明确要求生图、修图或批量处理时，不要先调用它；直接调用相应图片工具即可。

### `linghui_draw_image`

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `prompt` | string | 必填 | 图片描述，也可以包含预设名和追加要求。 |
| `count` | integer | `1` | 输出数量，最多受配置限制；只有用户明确要求多张时才增加。 |
| `aspect_ratio` | string | `auto` | 比例，如 `1:1`、`16:9`、`9:16`。 |
| `resolution` | string | `auto` | 清晰度，如 `1K`、`2K`、`4K`。 |

只用于生成其他角色、物体或场景。用户要 Bot 自拍、人设照片或合影时，必须改用 `linghui_persona_photo`；用户要修改已有图片时，必须改用 `linghui_edit_image`。

### `linghui_edit_image`

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `prompt` | string | 必填 | 编辑要求，不要无故加入预设名。 |
| `use_message_images` | boolean | `true` | 是否读取当前消息中的图片。 |
| `task_types` | string | `edit` | `edit` 为普通编辑；`figurine` 为手办化。 |
| `count` | integer | `1` | 生成不同版本的数量。 |
| `merge_multiple_images` | boolean | `false` | 多图默认分别处理；明确要求融合时才设为 `true`。 |

用户说“修改刚才那张”“继续改”时，即使当前消息没有图片，也可以调用；插件会从最近会话图片中寻找输入。

### `linghui_smart_generate`

参数：`user_request`（可选，string）。为空时使用当前消息。工具会读取当前消息、引用图片和最近会话上下文，自动选择文生图或图生图流程。用户只是闲聊或意图不明确时不要调用。

该工具会等待本次判断出的流程完成后返回工具结果；若希望提交后立即继续聊天，应直接使用 `linghui_draw_image`、`linghui_edit_image` 或并发批量工具。

### `linghui_persona_photo`

参数：`scene_hint`（场景提示）、`extra_request`（穿搭/表情/动作等要求）、`count`（数量，默认 1）、`photo_plan`（可选的逐张方向 JSON 字符串）。只有用户明确要求 Bot/人设角色的自拍、照片、外貌、穿搭、动作或合影时使用。LLM 可以自主在两个提示参数中安排穿搭、表情和动作，必须保留用户明确要求。插件直接尊重本次工具参数里的决定，日状态只作参考；可切回固定模式，不增加第二次 LLM 规划请求。`photo_plan` 格式为 `{"photos":[{"outfit":"蓝色外套","expression":"认真专注","gaze":"看镜头","pose":"倚栏杆"}]}`，每张一项，数量与请求张数一致；旧参数调用仍可用。完成后的文字会沿用当前 AstrBot 人格，后台模式下也一样。

### 批量工具

`linghui_batch_process` 和 `linghui_batch_process_concurrent` 都支持 `prompt`、`max_images`、`output_as_pdf`；并发版本额外接受 `concurrency`，但实际并发数仍受插件配置限制。批量失败策略可设置为“立即停止”“跳过继续”或“跳过但限次”，跳过的图片会按规则退还额度。

`linghui_batch_process` 当前会转交给并发批量流程；`linghui_batch_process_concurrent` 会提交后台任务并立即返回，完成后逐张发回或生成 PDF。

### `linghui_pack_images_to_pdf`

参数：`max_images`（integer，默认 10）。工具会等待正在进行的后台生图任务完成，再收集已有图片打包；它本身不会调用绘图渠道。

## 等待与后台模式

- `wait`：工具等待图片完成后再返回，适合后续回复必须立即引用结果的场景。
- `background`：提交任务后立即返回，Bot 可以继续聊天；完成后插件主动发送图片，适合群聊和耗时渠道。

执行模式的实际范围：普通单图文生图、单图图生图和单图人设拍照遵循此设置；批量并发工具始终提交后台任务；PDF 工具始终等待已有任务完成；智能判断工具会等待其分流流程完成。

后台完成文字由“后台完成消息文字”控制：`image_only` 只发图片，`name` 附带请求者和简短说明，`full` 附带完整成功信息。结果都会写入 Dashboard。

## 生成图片的文件路径

配置项“【智能工具与批量处理】向模型补送生成图片的缓存路径”（`llm_report_image_path`，默认开启）控制是否把刚生成图片的缓存位置告诉模型。

开启后：
1. `wait` 模式下，工具结果里会附带该图片缓存文件的文件名与完整绝对路径。
2. `background` 模式下，图片完成并写入缓存后，插件会在该会话的下一次模型请求里以临时上下文注入文件名和路径。
3. 路径只作为模型的内部参考，插件同时要求模型不要在回复里向用户展示路径、目录或文件名。

该功能适合“把刚生成的图继续处理、改名、发送或对比”这类需要引用本地文件的场景。关闭后模型不再收到任何文件路径。

## 冷却、额度与安全

- LLM 工具使用 `llm_cooldown_seconds`，按用户 ID 计时。
- 手动指令使用 `command_cooldown_seconds`，按“用户 ID + 群号”计时；两者互不影响。
- 每张图片通常消耗 1 次额度；批量任务按实际处理数量计算。
- 工具不会绕过群白名单、黑名单、私聊开关或用户权限。
- 结果和失败提示可按配置附带请求者标注；上游错误详情会自动隐藏域名和密钥。
