# 生图与图生图

## 端点

| 用途 | 端点 | 方式 |
|---|---|---|
| 文生图 | `POST /v1/images/generations` | 同步，直接返回图片 |
| 图生图 / 编辑 | `POST /v1/images/edits` | 同步，multipart 上传参考图 |
| 异步任务 | `POST /v1/images/tasks` | 返回任务，需轮询 |

**文生图与图生图是两个不同端点，图生图的字段形态还按模型家族不同**（见下一节）。
脚本按模型名自动选路，调用侧只需要 `--ref` 传参考图。

> `/v1/images/tasks` 是平台自研的异步入口，路由存在且可轮询；
> **当前脚本只用同步的两个端点**，没有接入异步。需要异步批量出图时，
> 按 [fallback-curl.md](fallback-curl.md) 的配方手工提交。

⚠️ **不要试图在 `/v1/images/generations` 的 JSON body 里传 `images` 数组来做图生图。**
网关会把它原样转发给上游，但**没有任何上游解释这个字段**：OpenAI 系要 multipart 的
`image` 文件；seedream 上游的字段是 `image`（单数，不是 `images`）；qwen 只读
`input.messages` 或 multipart 文件。更糟的是部分渠道会**静默丢弃**它并返回一张正常的
文生图——用户拿到一张没参考到参考图的图，却以为图生图成功了。

## 图生图怎么走（按模型家族）

| 家族 | 脚本默认 | 参考图字段 | 依据 |
|---|---|---|---|
| `gpt-image-*` / `chatgpt-image-*` / `dall-e-*` | multipart `POST /v1/images/edits` | 文件 `image`（多图重复 `image[]`） | OpenAI Images 编辑接口 |
| `qwen-image-*` / `wan2.*` | 同上 | 文件 `image` | ali / 电信 adaptor 的 multipart 分支 |
| `seedream-*` | 同上（**但见下**） | 文件 `image` | GateAI 文档化了 multipart 编辑 |
| `gemini-*image*` / `nano-banana` | **不支持** | — | gemini 图像模型不走 images 接口 |

`--ref` 素材处理：本地文件直接上传字节；远程 URL 会先下载再上传（multipart 只能传字节）。
`image` 模式下则是 URL 原样透传、本地文件转 data URL。

### seedream 的通道差异（2026-09-29 生产实测）

seedream 的参考图支持**取决于该模型当次路由到哪个上游通道**，三家行为不一致：

| 上游通道 | 图生图正确写法（据其契约） |
|---|---|
| Gate（gate.ai） | multipart `/v1/images/edits`；其 `/images/generations` 只吃 prompt |
| 电信（aigw.telecomjs.com） | JSON `/v1/images/generations` + `image`（URL 或 Base64）；edits 也收 multipart |
| 火山（volcengine） | JSON `/v1/images/generations` + `image`；edits 的 multipart 未接通，会丢文件 |

**实测结果（EasyAPI 生产环境，seedream-4.0）**：

| 投递方式 | 结果 |
|---|---|
| multipart `/v1/images/edits` | ❌ HTTP 404 `openai_error`（当前通道不接通这条路） |
| JSON `/v1/images/generations` + `image`（data URL） | ✅ HTTP 200，且**参考图确实生效**（输出保留了参考图里的黑框与蓝三角，只替换了指定元素） |

所以脚本对 **seedream 默认走 `image` 模式**。若某天平台调整了通道导致这条路失效，用 `--ref-mode edits` 换路：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.0 --prompt "..." --ref 参考图.png                   # 默认（image）
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.0 --prompt "..." --ref 参考图.png --ref-mode edits  # 换路
"$EASYAPI_SKILL_DIR/bin/easyapi" config --ref-mode edits   # 持久覆盖家族默认（整个部署统一走某条路）
```

**qwen-image 家族实测**（qwen-image-2.0）：multipart `/v1/images/edits` ✅ 成功，
且参考图确实生效（保留了参考图的构图，只替换了指定元素），与脚本默认一致。

`gpt-image` 家族未经实测（线上没有该系列模型），默认按 OpenAI Images 编辑接口的
multipart 契约走。

成功输出里会带 `ref_mode` 与 `endpoint`，说明这次实际走了哪条路。要一劳永逸，
需要在网关侧把三条通道的参考图行为对齐（火山接上 multipart、Gate 的 generations 认
`image`）——这属于服务端改动，不在客户端能力范围内。

## 响应结构

```json
{
  "created": 1710000000,
  "data": [
    { "url": "https://...", "b64_json": "...", "revised_prompt": "..." }
  ],
  "metadata": {}
}
```

`url` 与 `b64_json` 二者其一。网关会把结果转存到 CDN，所以 `url` 通常是稳定的。
脚本两种都能处理：有 `b64_json` 就解码落盘，否则下载 `url`。

## 常用参数

| 参数 | 说明 |
|---|---|
| `prompt` | 必填。GPT 图像模型上限 32000 字符 |
| `n` | 生成张数。**按次计费模型会按张数放大扣费** |
| `size` | 尺寸，如 `1024x1024`。GPT Image 支持任意 `WIDTHxHEIGHT`（边长需被 16 整除，宽高比 1:3 ~ 3:1） |
| `quality` | `low` / `medium` / `high` / `auto` |
| `background` | `transparent` / `opaque` / `auto`。用 `transparent` 需配 `output_format` 为 png 或 webp |
| `moderation` | `low` / `auto` |
| `output_format` | `png` / `jpeg` / `webp` |
| `output_compression` | 0–100，仅 webp / jpeg 生效 |

完整取值以 `"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型名>` 为准 —— 各模型的枚举与上下限不同。

参考图不走上面这张表：它由 `--ref` + 家族选路决定字段形态（见上一节），
不要手工往请求体里塞 `images`。

## 示例

```bash
# 最简
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "一只戴墨镜的柴犬，扁平插画风"

# 指定尺寸与质量
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "..." --size 1024x1024 --quality high

# 一次生成 4 张（按次计费模型会 ×4 扣费）
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "..." --n 4

# 带参考图生成
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "改成水彩风格" --ref 参考图.png

# 透明背景 PNG
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model gpt-image-2 --prompt "..." \
  --background transparent --output-format png

# 只看请求体不发请求（不需要 key，适合先确认参数）
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "..." --dry-run
```

## 输出

脚本把图片下载到 `./easyapi-out/<时间戳>-<模型>-<序号>.<扩展名>`，并打印**绝对路径**：

```
生图完成：qwen-image-3.0-pro（默认），共 2 张
  文件  D:\work\easyapi-out\20260921-153012-qwen-image-3.0-pro-1.png
  链接  https://cdn.example.com/xxx.png
```

**优先让用户在对话里看到图**：agent 支持内嵌本地图片时用 `![描述](绝对路径)` 直接展示，
有预览面板就主动打开；纯终端再退回 Markdown 文件链接。URL 始终附上，方便分享。
细节见 SKILL.md 的「把结果给用户看」。

**等待时间**：生图是同步接口，脚本默认最多等 **30 分钟**（`--timeout` 可调），
不再用 2 分钟的普通 HTTP 超时。agent 的 shell 超时上限较小时请用后台运行，别让 shell
先超时把生成判失败。

## 常见坑

1. **`n` 在按次计费模型上会成倍扣费。** 生成 4 张就是 4 倍价格。用户说「多来几张」之前先提醒成本。
2. **尺寸分隔符**：全角 `×`、`*`、大写 `X` 都会被上游拒绝。脚本会自动归一成 `x`，
   但只在整串确实是一个尺寸对时才改写，不会误伤提示词。
3. **`transparent` 背景必须配 png/webp**，配 jpeg 会被上游拒绝。
4. **`output_compression` 只对 webp/jpeg 生效**，配 png 是无效参数。
5. **参考图不要手工拼字段**：`images` 数组在 `/v1/images/generations` 上不生效，
   顶层 `first_frame_url` 之类更不行。统一用 `--ref`，由脚本按家族选路
   （详见上面的「图生图怎么走」）。
