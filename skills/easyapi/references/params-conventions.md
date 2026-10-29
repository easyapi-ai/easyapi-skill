# 统一参数命名约定

网关把各上游厂商的参数名统一成一套名字，客户端只用这套统一名，上游真实字段名的映射由网关完成
（`upstream_name` 只在管理端可见，客户端接口已经剥掉了）。

**这意味着：不要照抄厂商文档里的参数名。** 例如腾讯云点播写 `OutputConfig.Duration`，
统一名就是 `duration`；Kling 写 `aspectRatio`，统一名就是 `aspect_ratio`。

⚠️ **只有模型 `params` 里声明过的动态字段才会被网关转发。** `prompt` / `duration` /
`resolution` / `size` / `aspect_ratio` / `generate_audio` / `seed` / `image` / `images` /
`input_references` 这些是网关的一等字段，不声明也能用；其余参数（`negative_prompt`、
`cfg_scale`、`camera_control`、`watermark`、`sequential_image_generation`…）必须由模型
显式声明，否则会被静默丢弃。传之前用 `"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型>`
看它到底声明了什么。

## 图片参数

| 统一名 | 类型 | 说明 |
|---|---|---|
| `prompt` | string | 文本描述（几乎所有模型的必填项） |
| `n` | integer | 生成张数 |
| `size` | string | 输出尺寸，如 `1024x1024` |
| `quality` | string | 质量档，常见 `low` / `medium` / `high` / `auto` |
| `style` | string | 风格（主要 dall-e-3 用） |
| `response_format` | string | 返回方式 `url` / `b64_json` |
| `background` | string | 背景 `transparent` / `opaque` / `auto` |
| `moderation` | string | 审核强度 `low` / `auto` |
| `output_format` | string | 编码格式 `png` / `jpeg` / `webp` |
| `output_compression` | integer | 压缩率 0–100，仅 webp/jpeg 生效 |
| `partial_images` | integer | 流式中间图数量 0–3 |
| `stream` | boolean | 流式开关 |
| `images` / `image` | array / string | 参考图。**统一名归统一名，投递方式由脚本按模型家族决定**（见 [image.md](image.md)）；手工往 `/v1/images/generations` 塞 `images` 是无效的，各上游不认这个字段。用 `--ref` 让脚本处理 |
| `mask` | string | 编辑遮罩 |
| `input_fidelity` | string | 输入保真 |
| `watermark` | boolean | 水印开关 |
| `seed` | integer | 随机种子（部分模型） |
| `user` | string | 终端用户标识 |

## 视频参数

| 统一名 | 类型 | 说明 |
|---|---|---|
| `prompt` | string | 文本描述 |
| `image` | string | 单张输入图 |
| `images` | array | 多张输入图 |
| `size` | string | 精确尺寸，如 `1280x720` |
| `duration` | integer | 时长秒数 |
| `resolution` | string | 分辨率档，如 `720p` / `1080P` |
| `aspect_ratio` | string | 宽高比，如 `16:9` |
| `fps` | integer | 帧率 |
| `generate_audio` | boolean | 是否生成音频 |
| `seed` | integer | 随机种子 |
| `webhook_url` | string | 完成回调地址 |
| `input_references` | array | 结构化输入引用（见下） |
| `mode` | string | 生成模式（部分模型） |

厂商特有参数（`negative_prompt`、`cfg_scale`、`camera_control` 等）直接用文档名（camelCase 转
snake_case），通过 `--param key=value` 传。

## `input_references`：首尾帧与参考素材

首帧、尾帧、参考图/视频/音频**一律通过 `input_references` 表达**。

**不要用 `first_frame_url`、`last_frame_file_id`、`first_frame_image` 这类顶层参数** ——
它们不在网关的已知字段里，只会被丢进 metadata 然后被上游 DTO 丢弃，等于声明了一个传不动的参数。
这是最高频的「参数看起来传了但没生效」的原因。

每个元素只有三个字段，其余键在到达上游适配器之前就被丢弃：

| 字段 | 取值 | 说明 |
|---|---|---|
| `type` | `image` / `video` / `audio` | 必须与 `role` 的素材类型一致 |
| `url` | 字符串 | HTTPS URL、data URL，或裸 base64（不带 `data:` 前缀） |
| `role` | `first_frame` / `last_frame` / `reference_image` / `reference_video` / `reference_audio` | 素材用途 |

规则：

- **`last_frame` 必须与 `first_frame` 同时出现**，不能只传尾帧。
- 各上游适配器按 role 做大小写不敏感的子串匹配（first / last / video / audio / image）再映射到上游字段。
- 只能用文件 ID 交付的素材（无法转成 URL）不被收录，别尝试。

脚本已经封装好这些规则，直接用对应开关即可：

```bash
--first-frame 首帧.png          # -> role=first_frame
--last-frame 尾帧.png           # -> role=last_frame（必须配 --first-frame）
--ref-image 参考图.png          # -> role=reference_image
--ref-video 参考视频.mp4        # -> role=reference_video
--ref-audio 参考音频.mp3        # -> role=reference_audio
```

本地文件会自动转成 data URL。

## 参数名不一致时的排查顺序

1. `"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型名>` —— 看这个模型实际声明了哪些参数、哪些必填、枚举值是什么。
2. 如果 schema 里没有该参数，但厂商文档说有 —— 大概率是统一名不同，查上面的表。
3. 如果确认是厂商特有参数 —— 用 `--param key=value` 传，网关会透传到上游。
4. 加 `--strict` 可以让「不在 schema 里的参数」直接报错，用来排查拼写错误。
