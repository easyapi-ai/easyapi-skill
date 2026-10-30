---
name: easyapi
description: 通过 EasyAPI 模型网关生成图片、生成视频、文本与多模态对话，并查询可用模型、模型参数与价格。当用户要生成图片、生成视频、文生图、图生图、文生视频、图生视频、首尾帧生成视频、用 EasyAPI 调用模型、查询模型参数、比较模型价格、查询 API Key 余额额度时使用。Use when the user wants to generate images or videos, call the EasyAPI model gateway, look up model parameters, compare model prices, or check API key quota.
---

# EasyAPI 模型网关

所有网关请求都通过本 skill 的脚本发，**不要手工拼 curl 或 JSON**。

## 0. 先确定调用入口（每次会话第一步）

本文件所在目录记为 `EASYAPI_SKILL_DIR`。skill 可能被安装在任意位置（插件目录、
`~/.claude/skills`、`~/.codex/skills`…），所以**一律用绝对路径调用启动器**，
不要写 `python3 scripts/easyapi.py`——cwd 不一定是 skill 目录。

```bash
EASYAPI_SKILL_DIR="<本 SKILL.md 所在目录的绝对路径>"
"$EASYAPI_SKILL_DIR/bin/easyapi" env --network
```

`bin/easyapi` 会自己探测 `python3` → `python` → `py -3`，再执行 `scripts/easyapi.py`
（仅 Python 3.7+ 标准库，无需 pip）。`env --network` 一次输出：解释器与版本、
skill 目录、配置目录、内置快照日期、网关连通性、`/pricing/schema` 是否可用。

**没有 Python 时**：如实告诉用户，并按 `references/fallback-curl.md` 的固定配方调用。
降级路径不做参数预校验，必须**先查 schema 再发送**。

## 1. 两条纪律

### 凭据永远不进对话——但授权命令应当由你代跑

原则：**任何 key / 密码 / 令牌都不经过对话**；能由你代跑的就代跑，不要把终端操作推给用户。

**首选：你直接运行浏览器授权**。用户只需在弹出的网页里点一下「授权」，全程零终端输入：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" login --browser
```

- **Bash 超时给足 6 分钟**（脚本最长等 5 分钟授权，默认 2 分钟会打断用户操作）
- 没弹出浏览器时，把 stderr 里的「授权页地址」转成链接发给用户
- 平台未部署 CLI 授权端点时脚本会明确报「不支持网页授权」，此时改用密码模式
- 脚本与用户浏览器必须同机（桌面 agent 场景成立）；远程主机退回密码模式

**密码模式（兜底）**：`login` 需要交互输密码，而你的 shell 没有 TTY，密码经过你就等于
进入对话——这一种必须由用户在自己的终端运行：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" login
```

**唯一例外**：用户已主动把 key 贴在对话里。用 `login --api-key` 通过 stdin 写入，
然后**明确提醒他该 key 已出现在对话记录中，建议用完后到 https://www.easyapi.com 删除重建**。

### 不要让模型自己拼 JSON 或写轮询循环

脚本内置参数校验、异步轮询、结果下载、错误翻译；手工做这些正是出错的主要来源。

- 不确定请求长什么样 → 加 `--dry-run` 看脚本生成的请求体（不发请求、不需要 key）
- 生视频要等很久 → 脚本自己轮询到完成，**不要** `--no-wait` 后自己写循环

## 2. 按用户意图选命令

| 用户想做 | 命令 | 需要 key |
|---|---|---|
| 画图 / 改图 / 换风格 | `image` | ✅ |
| 生成视频 / 首尾帧 / 参考素材 | `video` | ✅ |
| 文字问答 / 看图问答 | `chat` | ✅ |
| 有哪些模型 / 哪个便宜 | `models` / `compare` | ❌ |
| 该用哪个模型（按能力+价格给候选） | `recommend` | ❌ |
| 某模型收哪些参数 | `schema` | ❌ |
| 配 key / 查余额 | `login` / `quota` | — |
| 环境或接口不对 | `env --network` | — |

通用开关：`--json`（机器可读）、`--refresh`（忽略缓存）、`--dry-run`（只校验不发请求）、
`--strict`（定义外的参数直接报错）、`--param key=value`（透传厂商特有参数）。

## 3. 模型怎么选、钱怎么花

**永远不要在多个候选里替用户拍板，也不要把几十个模型倒给用户。** 按这个顺序解析：

| 优先级 | 依据 | 行为 |
|---|---|---|
| 1 | 用户本轮点名（「用 Kling-3.0 做…」） | 原样使用；模型不存在就报错并列候选，**不回退默认** |
| 2 | 本次会话刚选过同类模型 | 沿用，并说一句「继续用刚才的 X」 |
| 3 | 本地默认（`config --default-video-model`） | 使用，并在结果里注明「模型：X（默认）」 |
| 4 | 什么都没配过 | **先不生成**：跑 `recommend` 给 3 个候选，问一次 |
| 5 | 用户说「随便 / 你定」 | 用返回里的 `recommended`，说明理由与预估价格，允许反悔 |

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" recommend --category video \
  --need first-last-frame --seconds 5 --resolution 720p
```

`recommend` 是**确定性排序**（参数声明里有没有对应能力 + 预估价格升序），不是画质评测，
所以别替它编「画质更好」这类结论。用户选定后顺带问一句「以后默认用它吗？」，同意就
`config --default-video-model <名字>`——第二次对话就变成「直接说需求」。

**花钱前确认（视频 / 多图 / 单价约 > $0.10）**：先 `--dry-run` 拿 `estimate_usd` 与
`effective_params`，给一行确认卡再正式提交：

```
Kling-3.0｜10 秒｜1080P｜16:9｜约 $0.90
直接发；要改说「10 秒 1080P」。
```

四条硬规则：

1. **用户点名模型就原样使用**，不许回退到他没指定的默认模型（模型名写错时如实报错）。
2. **用户没指定模型**：按上表解析；走到第 4 行时**提问/报错，绝不猜**。
3. **参数不确定就先看 schema**，不要试探性调用、不要凭记忆写参数名。
4. **省略参数优于猜参数**：脚本只填 schema 里显式声明的默认值。**也不要把提示词里的
   形容词翻译成参数**——用户说「高清」「长一点」，只在确认卡里把可选值摆出来让他选，
   不要自动变成 1080P/10 秒。

## 4. 首次使用：确认 key

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" quota
```

- 成功 → 直接干活
- 报「未配置 EasyAPI API Key」→ 按纪律代跑 `login --browser`，不要问用户要 key
- 报「账号下已有 N 个可用令牌，但没有名为 easyapi-skill 的专用令牌」→ 把令牌列表转述给
  用户让他选：复用（`--reuse-key 名字或ID`）还是新建（`--create-new`）。不要擅自新建或占用

`login` 自己也会先验证本地 key：能用就直接返回「无需重新授权」，不会再弹授权页。
只有换账号或明确要求重走授权时才加 `--force`。跨机器授权用 `login --browser --manual`，
授权页会停在原地展示授权码（不会自动回跳到一个连不上的本机地址）。

**新建的 key 默认绑定本机设备指纹**（指纹每次运行时现算，绝不写进配置文件）：即使配置被
拷到别的机器，key 也用不了。指纹变了会报 `client_binding_mismatch`：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" login --browser --rebind
```

`--rebind` 会新建一个绑定本机的 key，并在落盘后吊销配置里的旧 key。想给 key 设上限：
`login --browser --key-quota 20 --key-expire-days 90`（不填则不限量、永不过期）。
复用已有令牌时不改绑定（它可能还在别的脚本/机器上用）；要在多台机器用，就分别登录各建一个。

`models` / `schema` / `compare` / `recommend` 走匿名接口，**不需要 key**。

## 5. 配置完成后怎么回复用户

这是对话式 skill，配置成功后：

1. **不要给用户输出 CLI 命令让他去终端跑**——CLI 是你内部的实现细节。
2. **一两句话确认结果**：登录账号、令牌名、余额（有 `account_balance_usd` 就报账号余额；
   只有令牌视角就说「令牌不限量，消耗计入账号余额」）。
3. **给 2~3 个可以直接发的对话示例**：「帮我生成一只小猫，尺寸 1024x1024」
   「做一个猫在草地上跑的 5 秒视频」「EasyAPI 上有哪些生图模型，分别多少钱」。
4. 不贴 sk- key（哪怕打码）。

## 6. 生图

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model seedream-4.5 --prompt "一只戴墨镜的柴犬，扁平插画风"
```

输出是**本地文件绝对路径**加原始 URL。把**文件路径**用 Markdown 链接给用户，URL 附带用于分享。

```bash
... --prompt "改成水彩风格" --ref 参考图.png     # 图生图/改图（本地文件或 URL）
... --size 1024x1024 --quality high             # 用户明确要求时才指定
... --n 4                                        # 按次计费会按张数成倍扣费，先告知
```

**`--ref` 的投递方式由脚本按模型家族自动选**（seedream 走 JSON `image` 字段，
qwen-image / gpt-image 走 multipart `/v1/images/edits`），不要手工拼字段：

- **绝不要**手工往 `/v1/images/generations` 塞 `images` 数组——上游不认，部分渠道会静默
  返回一张没参考到参考图的文生图
- 输出里带 `ref_mode` / `endpoint`。用户反馈「没参考到」时用 `--ref-mode image|edits`
  换另一条路重试（详见 references/image.md）
- gemini 系图像模型不走 images 接口传参考图（脚本会明确报错）——需要图生图就换模型

## 7. 生视频

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" video --model Kling-3.0 --prompt "一只猫在草地上奔跑，电影感"
```

脚本提交任务、轮询到完成、下载结果并打印路径；进度在 stderr，不污染结果。
提交端点由模型的端点声明解析（Kling、seedance 等各有路径），解析不到回退 `/v1/videos`。

```bash
... --duration 10 --resolution 1080P              # 规格按 schema 的可选值，别自造
... --first-frame 首帧.png                        # 首帧驱动
... --first-frame 首帧.png --last-frame 尾帧.png    # 首尾帧（尾帧不能单独出现）
... --ref-image a.png --ref-image b.png           # 参考素材
```

视频多为**按秒计费**：10 秒 1080P 大约是 5 秒 720P 的 4 倍。用户要「长一点、清晰一点」时
先说清成本变化，或先跑 `compare` 看价格。

## 8. 查参数（不确定就查，别猜）

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" schema seedream-4.5
```

输出包含：分类、参数来源、**提交端点**、必填项、参数表（类型/必填/默认/枚举/范围）、
**一个可直接发送的示例请求体**。`--json` 给结构化字段（`params`、`required`、`example`、
`submit_endpoint`、`schema_sources`）。

参数来源按优先级，并且**逐字段合并**：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `/pricing/schema?all=1` 全量包 | 一次拉全量，新模型上线自动生效；缓存 24h，`--refresh` 强刷 |
| 2 | `/pricing/schema?model_name=` 单模型 | 只有单模型接口的部署 |
| 3 | `/pricing/detail` 的 `params` | 更老的部署 |
| 4 | 内置 `references/model-snapshot.json` | 完全离线兜底 |

高优先级覆盖同名参数，低优先级补缺失参数。若输出提示「线上未声明 N 个参数，已用内置快照
补全」，说明这几个字段只有快照有定义：取值校验照做，但**未声明的动态字段可能被网关丢弃**，
报错时以 schema 里线上声明的字段为准。

> 注意：`env --network` 显示 `schema 接口不可用` 时，说明网关还没部署新接口，
> 会走 3/4 两级回退——功能不受影响，只是拿不到实时的新模型参数。

### 参数写错时的排查顺序

1. `schema <模型名>` 看正确参数名与取值
2. 厂商文档里的参数名（如 `aspectRatio`）不能直接用，网关用统一名（`aspect_ratio`）
3. 确认是厂商特有参数 → `--param key=value` 透传（值会按 JSON 解析）
4. 加 `--strict` 让「不在定义里」的参数直接报错，用来抓拼写错误

## 9. 模型选择

**不要自己猜模型名。** 先查：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" models --category image
"$EASYAPI_SKILL_DIR/bin/easyapi" compare --category video --seconds 5
```

模型解析顺序：`--model` → 配置默认 → **报错并列出候选**。最后一种把候选列给用户，
让他二选一：本次用 `--model <模型名>`，或设为默认
`config --default-video-model <模型名>`。

模型选择明显影响价格或质量时（高清 vs 快速、时长），先给简短对比再让用户定。

## 10. 结果与错误

- **进度**在 stderr，**结果**在 stdout。`--json` 得到单个对象（`ok` / `files` / `path` /
  `task_id` / `endpoint` / `notes` / `warnings`）。
- 失败时退出码非 0，`--json` 下有 `ok: false` + `error` + `hint`。**把 `hint` 转达给用户**，
  它通常就是解决办法。
- 视频超时不会丢任务：错误里带任务 ID 与查询地址，可稍后手工续查或加大 `--timeout`。
- 本地校验失败发生在发请求之前（`--dry-run` 同样会校验），所以参数错误不会白扣费。
- `client_binding_mismatch` = key 绑在另一台设备上（换机、改主机名、拷配置都会这样）。
  不要重试、不要换 key 乱试，直接告诉用户需要 `login --browser --rebind`。

## 11. 按需阅读

| 文件 | 什么时候读 |
|---|---|
| [references/image.md](references/image.md) | 生图参数细节、尺寸/质量/背景、参考图投递 |
| [references/video.md](references/video.md) | 生视频参数、首尾帧、轮询与计费细节 |
| [references/chat.md](references/chat.md) | 文本与多模态对话 |
| [references/models.md](references/models.md) | 计费方式与公式、比价陷阱 |
| [references/params-conventions.md](references/params-conventions.md) | 参数名对不上、首尾帧传了不生效 |
| [references/account.md](references/account.md) | key 配置、额度查询、只用 key 能做什么 |
| [references/troubleshooting.md](references/troubleshooting.md) | 报错时按状态码与原因查 |
| [references/fallback-curl.md](references/fallback-curl.md) | 没有 Python 时的降级配方 |

脚本入口：`bin/easyapi`（启动器）→ `scripts/easyapi.py`（仅标准库）。
