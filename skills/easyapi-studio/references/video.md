# 生视频

## 端点与流程

生视频**全部是异步任务**：提交拿到任务 ID，再轮询到完成。

| 步骤 | 端点 |
|---|---|
| 提交 | 由模型的端点声明解析；解析不到回退 `POST /v1/videos` |
| 轮询 | 提交路径 + `/{task_id}`（如 `GET /v1/videos/{task_id}`） |
| 取内容 | `GET /v1/videos/{task_id}/content`（直接流式返回视频字节） |

后端把 `POST /v1/videos` 与 `POST /v1/video/generations` 都接到同一个任务处理器，
所以回退是安全的；模型若声明了官方形态（如 `/kling/v1/videos/text2video`、
`/seedance/...`），脚本会优先用它，轮询也跟着同一条路径。
`"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型>` 输出的「提交端点」就是脚本实际会用的路径。

**轮询由脚本内置完成**，不要自己写轮询循环 —— 提交后脚本会一直等到 `completed` 或 `failed`，
然后下载结果并打印路径。这是刻意设计：手写轮询是仅次于拼错参数的第二个高频出错点
（间隔不合理、状态判断不全、超时没兜底）。

轮询行为：起始 3 秒，按 1.3 倍退避，上限 10 秒；总超时默认 900 秒，用 `--timeout` 调整。
超时不会丢任务 —— 错误信息里会带上任务 ID 和查询地址，可以稍后手工续查。
agent 的 shell 超时上限小于 15 分钟时用后台运行，不要自己写轮询循环。

**结果预览**：下载到本地后优先在对话里播放（agent 支持视频/附件时）或打开它的预览面板，
纯终端再给文件链接 + URL。细节见 SKILL.md 的「把结果给用户看」。

## 轮询响应

```json
{
  "code": 200,
  "msg": "",
  "data": {
    "job_id": "...",
    "status": "pending | in_progress | completed | failed",
    "model": "Kling-3.0",
    "message": "失败原因",
    "status_url": "...",
    "download_url": "https://...",
    "duration": 5,
    "resolution": "1080P",
    "aspect_ratio": "16:9",
    "generate_audio": true,
    "estimated_cost": "0.4500",
    "billed_cost": "0.4464",
    "billing_status": "settled",
    "created_at": "...",
    "completed_at": "..."
  }
}
```

`download_url` 在 `completed` 后出现。它可能指向 CDN 或上游直链；如果缺失，脚本会回退到
`/v1/videos/{task_id}/content`。

## 常用参数

| 参数 | 说明 |
|---|---|
| `prompt` | 文本描述。**用参考文件或场景类型时可能不是必填** —— 以 schema 为准 |
| `duration` | 时长秒数。范围与枚举各模型不同，**先 `schema` 再看** |
| `resolution` | 分辨率档，如 `720P` / `1080P`。**注意大小写，各模型枚举不同** |
| `aspect_ratio` | 宽高比，如 `16:9` / `9:16` / `1:1` |
| `size` | 精确尺寸，如 `1280x720` |
| `generate_audio` | 是否生成音频 |
| `seed` | 随机种子 |
| `input_references` | 首尾帧与参考素材（见下） |

脚本只填 schema 里**显式声明**的默认值。像 Kling 这类线上声明了 `duration=5`、`resolution=720P`、
`aspect_ratio=16:9`、`generate_audio=false` 的模型，请求体里会自动带上这些值（与上游默认一致）；
没有声明的参数不会凭空补，交给上游决定。

## 首尾帧与参考素材

用 `--first-frame` / `--last-frame` / `--ref-image` / `--ref-video` / `--ref-audio`，
脚本会组装成正确的 `input_references`：

```bash
# 首帧驱动
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "镜头缓慢推进" --first-frame 首帧.png

# 首尾帧过渡
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "平滑过渡" \
  --first-frame 首帧.png --last-frame 尾帧.png

# 多参考图
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model seedance-2.0 --prompt "..." \
  --ref-image a.png --ref-image b.png
```

**尾帧不能单独出现** —— 必须同时给首帧，否则上游会拒绝。脚本会在本地就拦下来。

⚠️ **不要用 `first_frame_url` / `last_frame_file_id` 这类顶层参数**。它们不在网关已知字段里，
会被静默丢弃，表现为「参数传了但没生效」。详见 [params-conventions.md](params-conventions.md)。

## 示例

```bash
# 最简
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "一只猫在草地上奔跑，电影感"

# 指定时长与分辨率
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "..." --duration 10 --resolution 1080P

# 提交后不等，稍后自己查
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "..." --no-wait
# 返回任务 ID，之后可手工查：GET https://token.easyapi.com/v1/videos/<task_id>

# 先确认参数再发（不需要 key）
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "..." --duration 10 --dry-run
```

## 计费

视频多为**按秒计费**，价格随时长与分辨率变化：

```
费用(USD) = 时长秒数 × model_ratio × video_per_second_ratio(分辨率) × 分组倍率
```

所以 **10 秒 1080P 大约 5 秒 720P 的 4 倍**。用户要求「长一点、清晰一点」时，先说明成本变化，
或先跑 `"$EASYAPI_SKILL_DIR/bin/easyapi" compare --category video --seconds 10 --resolution 1080p` 让用户看价格再决定。

任务提交时会按 `DefaultVideoEstimateTokens = 20000` 预扣，完成后按上游实际用量差额结算 ——
轮询结果里的 `estimated_cost` 是预扣，`billed_cost` 才是实际扣费。

## 常见坑

1. **分辨率枚举大小写不统一。** Kling 是 `720P`/`1080P`，其他模型可能是 `720p`。
   脚本会做大小写不敏感匹配并回写成模型定义的标准写法，非法值会直接报错并列出可选值。
2. **计费倍率表的分辨率键不统一。** 线上确实存在 `720p` 和 `720`（不带 p）两种写法，
   所以 `compare` 可能对某些模型报「该分辨率没有按秒倍率」—— 换一个 `--resolution` 值即可。
3. **失败任务会退款**，`billed_cost` 为 0，`billing_status` 为 `settled`。
4. **`duration` 类型是数字不是字符串**，传 `"5"` 会被脚本转成 `5`；直接拼 JSON 时容易写成字符串。
