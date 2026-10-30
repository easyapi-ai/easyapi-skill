# API Key 配置与额度查询

## 配置 Key

**任何 key / 密码 / 令牌都不进对话**——贴在对话里的内容会永久留在记录中。
在此基础上，能由 agent 代跑的就代跑：

**首选：agent 直接运行浏览器授权**（Bash 超时给足 6 分钟，脚本最长等 5 分钟授权）：

```bash
EASYAPI_SKILL_DIR="<本 SKILL.md 所在目录>"
"$EASYAPI_SKILL_DIR/bin/easyapi" login --browser
```

脚本自动打开授权页，用户点一下「授权」即完成——脚本自动创建/复用 API Key、
写入 `~/.easyapi/config.json`（权限 600）、查余额验证。用户全程零终端输入、
不接触 key；浏览器没弹出时把 stderr 里的「授权页地址」转成链接发给用户。

### 四种登录方式

| 命令 | 适合谁 | 谁来跑 |
|---|---|---|
| `login --browser` | **首选**。浏览器点一下「授权」，不输任何密码。平台需部署了 CLI 授权端点（`/cli/*`，脚本会自动探测） | **agent 直接跑** |
| `login` | 账号密码登录；两步验证账号会提示输验证码。浏览器授权不可用时的兜底 | 用户自己的终端 |
| `login --access-token` | 不想输密码且无浏览器授权。到控制台 `/setting` 的「安全设置 → 系统访问令牌」复制令牌，同页可看用户 ID | 用户自己的终端 |
| `login --api-key` | 已有 sk- key，直接粘贴（跳过账号） | 用户自己的终端（key 走 stdin） |
| 环境变量 `EASYAPI_API_KEY` | CI / 脚本化环境 | — |

浏览器授权的细节：脚本在本机 `127.0.0.1` 起一次性监听，打开控制台授权页
`/cli/authorize`，已登录用户点「授权」后页面 302 回本机监听，脚本凭一次性授权码
（2 分钟有效）换取登录态。回调地址**只允许本机回环**，后端强制校验——授权码不可能
被引到外部服务器。跨机器场景用 `login --browser --manual`：把授权页显示的授权码
手动粘贴回来即可。

### 自动创建的 key 是什么样的

- 名字固定为 `easyapi-skill`（可用 `--key-name` 改），默认不限额度、永不过期——额度本来
  就受账号余额约束，符合「这个 key 就是我账号」的直觉。想收紧就加
  `--key-quota 20`（美元额度上限）和 `--key-expire-days 90`（有效期）；
- **重复登录复用同一个 key**，不会每次登录都新建；
- **账号下已有其它可用令牌、但没有 `easyapi-skill` 时，不会静默新建**——脚本会停下来
  把已有令牌列出来，让 agent 问用户选哪个：`--reuse-key 名字或ID` 复用，或
  `--create-new` 新建专用令牌（便于单独吊销）。全新账号（一个令牌都没有）才自动创建；
- 同名 key 被禁用时**不会擅自启用**，会明确提示到控制台处理；
- 触发令牌数量上限时，会提示删掉不用的令牌或用 `--key-name` 换名。
- 登录成功时脚本会用一次性登录态查一次**账号余额**一并报出（不限量令牌自身的
  `/usage/token` 余额字段是无意义的，账号余额 sk- key 查不到，只有登录那一刻能拿到）。

### 设备绑定

新建的 key 默认绑定本机设备指纹（`client_id`，64 位十六进制）。指纹在每次运行时现算
（Windows `MachineGuid`、Linux `machine-id`、macOS `IOPlatformUUID`；都取不到才退回
MAC + 主机名），**不写进配置文件**。含义：

- 配置被拷到别的机器 → 请求返回 `client_binding_mismatch`，key 用不了；
- 换机 / 改主机名 / 还原系统 → 用 `login --browser --rebind`：新建绑定本机的 key，
  落盘成功后吊销配置里的旧 key；
- `--reuse-key` 复用已有令牌时**不改绑定**（它可能还在别的脚本/机器上用）；
- 多台机器要同时用，就在每台各登录一次、各建一个 key；不想要绑定用 `--no-bind`；
- 查询当前指纹前缀：`config` 或 `env` 的「设备指纹」一行（只给前 12 位）。

### 密码与登录令牌的安全边界

- **登录密码、系统访问令牌、授权票据只在内存里用一次，绝不写盘。**
  落盘的只有权限有限的 sk- key；
- key 不在命令行参数里、不在输出里出现任何字符（连打码片段也不输出）；
- 密码输入不回显、不进 shell history；
- 浏览器授权的授权码一次性、2 分钟有效，且回调地址仅限本机回环（后端强制校验）；
- 平台开启网页人机验证（Turnstile/阿里云验证码）时，命令行密码登录会被拒绝并明确提示，
  此时用 `--browser` 或 `--access-token`；
- 平台关闭密码登录时同理。

### key 的读取优先级

1. 环境变量 `EASYAPI_API_KEY`
2. 配置文件 `~/.easyapi/config.json` 的 `api_key`

**没有第三种。** 脚本刻意不支持命令行参数传 key，因为那会进入 shell history 和进程列表。

### 如果用户已经在对话里贴了 key

不要假装没看到。按这个顺序处理：

1. 用 `login --api-key` 把它写进配置文件（通过 stdin 传入，不要写进命令行参数）。
2. **明确告诉用户：这个 key 已经出现在对话记录里，建议用完后到
   https://token.easyapi.com 的「令牌」页面删除或重建。**
3. 后续对话不要再复述这个 key。

## 额度查询

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" quota
```

输出总额度、已用、剩余、过期时间和模型白名单（如果启用了）。

```
令牌名称        t
总额度          $10.0000
已用            $2.0000
剩余            $8.0000
过期时间        永不过期
```

换算关系：`1 USD = 500,000 quota`。

## 只用 API Key 能做什么、不能做什么

这点很关键，避免向用户承诺做不到的事：

| 能力 | 端点 | 只用 sk- key 可以吗 |
|---|---|---|
| 查额度 / 用量 | `GET /usage/token` | ✅ 可以 |
| 模型列表 | `GET /pricing` | ✅ 可以（**且匿名即可，不需要 key**） |
| 模型参数 | `GET /pricing/detail` | ✅ 可以（**匿名即可**） |
| 生图 / 生视频 / 对话 | `/v1/...` | ✅ 可以 |
| 用户资料 | `GET /user/self` | ❌ 需要登录会话 |
| 消费日志 | `GET /log/self` | ❌ 需要登录会话 |
| 用量统计 | `GET /data/self` | ❌ 需要登录会话 |
| 令牌管理 | `/token/*` | ❌ 需要登录会话 |

后四项走的是 `UserAuth`（登录会话鉴权），不是 API Key 鉴权，所以脚本没有实现。
用户想查消费明细，引导他去 https://token.easyapi.com 控制台。

## 其他配置

```bash
# 查看当前配置（key 会打码）
"$EASYAPI_SKILL_DIR/bin/easyapi" config

# 设置默认模型，之后可以不传 --model
"$EASYAPI_SKILL_DIR/bin/easyapi" config --default-image-model seedream-4.5
"$EASYAPI_SKILL_DIR/bin/easyapi" config --default-video-model Kling-3.0
"$EASYAPI_SKILL_DIR/bin/easyapi" config --default-text-model deepseek-v4.1-flash

# 清空模型与参数缓存（模型有变动时用）
"$EASYAPI_SKILL_DIR/bin/easyapi" config --clear-cache

# 移除已保存的 key
"$EASYAPI_SKILL_DIR/bin/easyapi" logout
```

## 网关入口

**接口和控制台是两个域名**，脚本都内置了默认值：

| 用途 | 默认值 | 环境变量 |
|---|---|---|
| 接口（`/v1/...`、`/pricing`、`/usage/token`） | `https://token.easyapi.com` | `EASYAPI_BASE_URL` |
| 控制台前端（浏览器授权页所在） | `https://www.easyapi.com` | `EASYAPI_CONSOLE_BASE_URL` |

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" config --base-url https://your-gateway.example.com
"$EASYAPI_SKILL_DIR/bin/easyapi" config --console-base https://your-console.example.com
```

⚠️ **两者不能混用。** 浏览器授权页 `/cli/authorize` 是前端路由（控制台域），
而票据签发 `POST /cli/authorize`、兑换 `POST /cli/exchange` 是接口（网关域）——
路径同名、域名不同。用网关域名开页面会打到只接受 POST 的接口上（GET 404），
脚本会提前探测并明确报错，提示用 `--console-base` 修正。

只有当定价接口被拆到另一个域名时，才需要额外设置 `--pricing-base`（不设跟随网关入口）。

⚠️ **不要把接口入口配成 `api.easyapi.com`。** 那个域名不是网关（是一个普通 Java 服务，
所有网关路由都返回 `No static resource`）。脚本识别到这种响应会直接告诉你地址配错了。

## 状态文件位置

```
~/.easyapi/
├── config.json          # key、base_url、默认模型（权限 600）
└── cache/
    ├── pricing.json     # 模型列表缓存
    └── detail/*.json    # 单模型参数缓存
```

用 `EASYAPI_HOME` 环境变量可以整体改到别处（CI 或隔离测试时有用）。

**注意：这些状态不在 skill 目录里。** skill 目录可能只读，插件缓存更新时还会被整体替换，
所以配置与缓存必须放在用户目录。
