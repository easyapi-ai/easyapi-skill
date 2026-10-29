# 没有 Python 时的降级配方（curl）

本 skill 的主路径是 `bin/easyapi`（Python 3.7+ 标准库）。如果用户机器上没有 Python，
按下面四条配方手工调用；**降级路径不做任何参数预校验**，所以必须先用接口确认参数。

先把 key 放进环境变量，**不要写进命令、也不要贴进对话**：

```bash
read -rsp "EasyAPI key: " EASYAPI_KEY && export EASYAPI_KEY
BASE=https://token.easyapi.com
```

降级前先明确告诉用户：「这次跳过了本地参数校验，参数名/取值不对会等到上游报错」。

## 1. 列模型（匿名）

```bash
curl -s "$BASE/pricing" | python -m json.tool     # 有 python 时
curl -s "$BASE/pricing"                            # 没有 python 就让 agent 直接读返回的 JSON
```

## 2. 查单个模型的参数（匿名，优先）

```bash
curl -s "$BASE/pricing/schema?model_name=Kling-3.0"
# 返回 data.request 是 OpenAPI 3.1 Schema Object：properties + required
# 老部署没有这个接口时，退回详情接口：
curl -s "$BASE/pricing/detail?model_name=Kling-3.0"
# data.params[分类].properties 是同一份参数定义（字段名是 min/max/min_length）
```

要完整 OpenAPI 文档（可直接喂给校验器/代码生成器）：

```bash
curl -s "$BASE/pricing/schema?model_name=Kling-3.0&format=openapi"
```

**必须把 `required`、`enum`、`minimum/maximum` 读准再发下一个请求。** 这是降级路径唯一
的安全网：本地不校验，就靠调用前先看 schema。

## 3. 生图（同步）

```bash
curl -s -X POST "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $EASYAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedream-4.5","prompt":"一只戴墨镜的柴犬","size":"1024x1024"}'
```

返回 `data[].url` 或 `data[].b64_json`；`url` 有时效，拿到后立刻下载：

```bash
curl -s -o "out-$(date +%Y%m%d_%H%M%S).png" "<url>"
```

图生图（参考图）按模型家族分两条路，**不要往 generations 里塞 `images` 数组**：

```bash
# GPT / qwen / wan 系：multipart edits，单图用 image，多图重复 image[]
curl -s -X POST "$BASE/v1/images/edits" \
  -H "Authorization: Bearer $EASYAPI_KEY" \
  -F "model=gpt-image-1" \
  -F "prompt=改成水彩风格" \
  -F "image=@参考图.png"

# seedream 系：JSON 的 image 字段（本地文件要转 data URL）
curl -s -X POST "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $EASYAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedream-4.5","prompt":"改成水彩风格","image":"https://example.com/ref.png"}'
```

## 4. 生视频（异步：提交 → 轮询 → 下载）

```bash
# 提交
curl -s -X POST "$BASE/v1/videos" \
  -H "Authorization: Bearer $EASYAPI_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"Kling-3.0","prompt":"一只猫在草地上奔跑，电影感","duration":5,"resolution":"720P","aspect_ratio":"16:9"}'
```

拿到任务 ID（响应里的 `id` / `task_id`，可能在 `data` 下），然后：

```bash
# 轮询：completed 后取 download_url；未完成时等 5–15 秒再查
curl -s "$BASE/v1/videos/<task_id>" -H "Authorization: Bearer $EASYAPI_KEY"

# 下载（没有 download_url 时走内容代理）
curl -s -o "video-$(date +%Y%m%d_%H%M%S).mp4" "$BASE/v1/videos/<task_id>/content" \
  -H "Authorization: Bearer $EASYAPI_KEY"
```

失败状态（`failed` / `error` / `cancelled`）时把上游返回的 `message` 原样转述给用户。

> 提交路径也可能是 `/v1/video/generations` 或模型自己的官方路径（如
> `/kling/v1/videos/text2video`）。以 `schema` 输出里的 `submit_endpoint` /
> `endpoints` 为准；轮询路径 = 提交路径 + `/{task_id}`。

## 降级路径要额外注意

1. **参数名用统一名**：`aspect_ratio` 不是 `aspectRatio`，`duration` 不是 `video_length`。
   厂商私有参数（`negative_prompt`、`cfg_scale`、`camera_control`）只有模型声明过才会被转发。
2. **首尾帧只走 `input_references`**，元素固定 `{type,url,role}`；`last_frame` 必须和
   `first_frame` 一起出现。顶层的 `first_frame_url` 之类会被静默丢弃。
3. **分辨率大小写**：`720P` / `1080P` 与 `720p` 各模型不同，照 schema 的 `enum` 抄。
4. 用户若经常用这个 skill，建议引导他装 Python 3.7+，主路径才有本地校验与轮询兜底。
