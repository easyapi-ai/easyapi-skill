---
name: easyapi-auth
description: 配置 EasyAPI 账号与 API Key：浏览器授权登录、自动创建或复用专属令牌、设备绑定与换绑、查询余额额度、查看本地环境与配置。当用户要配置或登录 EasyAPI、获取或更换 API Key、查 key 的余额额度、排查「未配置 EasyAPI API Key」时使用。Use when the user wants to sign in to EasyAPI, configure or rotate an API key, check key quota, or fix missing-credential errors.
---

# EasyAPI 登录与凭据

这个技能只做一件事：把 EasyAPI 的 key 配好、验证通过，并告诉用户配在哪。
生成图片/视频、查模型与参数属于 `easyapi-studio` 技能，这里不碰那些命令。

## 0. 入口

本文件所在目录记为 `EASYAPI_SKILL_DIR`。skill 可能装在任意位置，一律用绝对路径：

```bash
EASYAPI_SKILL_DIR="<本 SKILL.md 所在目录的绝对路径>"
"$EASYAPI_SKILL_DIR/bin/easyapi-auth" env --network
```

`bin/easyapi-auth` 会自动探测 `python3` → `python` → `py -3`。`env --network` 输出解释器、
skill 目录、配置目录、设备指纹前缀、网关连通性、`/pricing/schema` 是否可用。

**没有 Python 时**：如实告诉用户，并让他安装 Python 3.7+（只用标准库，不需要 pip）。
登录流程依赖本地 loopback 回调，没有 Python 无法降级。

## 1. 两条纪律

### 凭据永远不进对话，但授权命令由你代跑

**任何 key / 密码 / 令牌都不经过对话**；能由你代跑的就代跑。

**首选：你直接运行浏览器授权**。用户只需在弹出的网页里点一下「授权」，全程零终端输入：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi-auth" login --browser
```

- **Bash 超时给足 6 分钟**（脚本最长等 5 分钟授权，默认 2 分钟会打断用户操作）
- 没弹出浏览器时，把 stderr 里的「授权页地址」转成链接发给用户
- 平台未部署 CLI 授权端点时脚本会明确报「不支持网页授权」，此时改用密码模式
- 脚本与用户浏览器必须同机；远程主机用 `--manual`

**密码模式（兜底）**：`login` 需要交互输密码，而你的 shell 没有 TTY，密码经过你就等于
进入对话。这一种必须由用户在自己的终端运行：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi-auth" login
```

**唯一例外**：用户已主动把 key 贴在对话里。用 `login --api-key` 通过 stdin 写入，然后
**明确提醒他该 key 已出现在对话记录中，建议用完后到控制台删除重建**。

### 不要替用户决定用哪个已有令牌

**新建的令牌默认绑定本机设备指纹**，所以不要拿 `--reuse-key` 去占用用户别处的 key。

## 2. 命令

| 命令 | 作用 | 需要 key |
|---|---|---|
| `env [--network]` | 解释器 / 目录 / 指纹 / 网关与接口能力 | ❌ |
| `login [方式]` | 登录并配置 key（本地 key 可用时直接返回） | — |
| `logout` | 移除本地保存的 key | — |
| `quota` | 令牌额度、用量、过期时间 | ✅ |
| `config` | 查看或修改网关地址、默认模型、缓存 | — |

## 3. 登录方式

| 方式 | 命令 | 谁来跑 |
|---|---|---|
| 浏览器授权（推荐） | `login --browser` | **agent 直接跑** |
| 跨机器 / 无浏览器 | `login --browser --manual`，把授权页上的授权码粘贴回来 | 用户 |
| 账号密码 | `login` | 用户自己的终端 |
| 控制台访问令牌 | `login --access-token` | 用户自己的终端 |
| 已有 key | `login --api-key` | 用户自己的终端（key 走 stdin） |

`login` 会先验证本地 key：仍然可用就直接返回「无需重新授权」，不会再弹授权页。
换账号或要强制重走授权时加 `--force`。

**`--manual` 模式**：授权页会停在原地展示授权码（不会自动回跳到连不上的本机地址），
复制粘贴回命令行即可。

## 4. 设备绑定

新建的 key 默认绑定本机设备指纹（指纹每次现算，不写进配置）：即使配置被拷到别的机器，
key 也用不了。指纹变了会报 `client_binding_mismatch`：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi-auth" login --browser --rebind
```

`--rebind` 会新建一个绑定本机的 key，并在落盘后吊销旧 key。想给 key 设上限：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi-auth" login --browser --key-quota 20 --key-expire-days 90
```

不填则不设限。复用已有令牌（`--reuse-key`）不会改绑定；多台机器请分别登录各建一个 key。
不想要绑定时加 `--no-bind`。

## 5. 配置完成后怎么回复用户

这是对话式技能，配置成功后：

1. **不要给用户输出 CLI 命令让他去终端跑**，CLI 是你内部的实现细节。
2. **一两句话确认**：登录账号、令牌名、余额（有 `account_balance_usd` 就报账号余额；
   只有令牌视角就说「令牌不限量，消耗计入账号余额」）。
3. **告诉他接下来能做什么**：生成图片/视频、查模型与价格由 `easyapi-studio` 技能负责，
   可以给 2~3 个示例：「帮我生成一只小猫」「做一个 5 秒的猫跑步视频」「有哪些生图模型」。
4. 不贴 key（哪怕打码）。

## 6. 常见错误

| 现象 | 处理 |
|---|---|
| `未配置 EasyAPI API Key` | 直接代跑 `login --browser`，不要问用户要 key |
| 账号下已有可用令牌、但没有 `easyapi-skill` | 把令牌列表转述给用户，让他选 `--reuse-key` 或 `--create-new` |
| `client_binding_mismatch` | 换机/改主机名导致，跑 `login --browser --rebind`，不要重试 |
| 密码登录被人机验证拦住 | 改用 `login --browser` 或 `login --access-token` |
| 网络失败 | 不要逼用户重新授权；`env --network` 确认网关是否可达 |

脚本入口：`bin/easyapi-auth`（启动器）→ `scripts/easyapi.py`（仅标准库）。
