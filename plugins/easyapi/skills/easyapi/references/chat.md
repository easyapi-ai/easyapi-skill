# 文本与多模态对话

## 端点

| 端点 | 协议 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容（默认用这个） |
| `POST /v1/messages` | Anthropic 兼容 |
| `POST /v1/responses` | OpenAI Responses |

脚本走 `/v1/chat/completions`，非流式。

## 示例

```bash
# 最简
"$EASYAPI_SKILL_DIR/bin/easyapi" chat --model deepseek-v4.1-flash --prompt "用一句话解释量子纠缠"

# 带系统提示词
"$EASYAPI_SKILL_DIR/bin/easyapi" chat --model deepseek-v4.1-flash \
  --system "你是简洁的技术编辑，回答不超过三句话" \
  --prompt "解释一下什么是反向代理"

# 多模态：看图
"$EASYAPI_SKILL_DIR/bin/easyapi" chat --model deepseek-v4.1-flash \
  --prompt "这张图里有什么？" --image 截图.png

# 多图
"$EASYAPI_SKILL_DIR/bin/easyapi" chat --model glm-5.3-flash --prompt "对比这两张图" \
  --image a.png --image b.png

# 控制输出
"$EASYAPI_SKILL_DIR/bin/easyapi" chat --model deepseek-v4.1-flash --prompt "..." \
  --max-tokens 2000 --temperature 0.3
```

`--image` 接受 URL 或本地文件路径，本地文件会自动转成 data URL。

## 模型选择

文本模型**按量计费**（token），无法本地估算价格。用
`"$EASYAPI_SKILL_DIR/bin/easyapi" models --category text` 看可用模型，用 `"$EASYAPI_SKILL_DIR/bin/easyapi" compare --category text`
看基础倍率排序 —— 倍率低的不一定便宜，因为输出倍率（`completion_ratio`）和上下文长度
都会影响总价。

选模型时优先考虑：能力是否够用、上下文窗口、是否支持工具调用与视觉输入。
价格差异在小规模使用下通常不是决定因素。

## 响应

```json
{
  "choices": [
    { "message": { "role": "assistant", "content": "..." }, "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 48, "total_tokens": 60 }
}
```

脚本输出正文，并在末尾附 token 用量。

## 注意

- **脚本不做流式输出**。流式对 agent 场景意义不大（拿到的还是完整文本），
  而且会让输出解析复杂化。需要流式请直接用 API。
- **长文本别用 `--prompt` 硬塞**。命令行参数有长度限制（Windows 约 32KB），
  更长的内容建议让用户写进文件，然后由 agent 读取后自行组织。
- `--param` 可以传任意额外字段，如 `--param top_p=0.9`。
