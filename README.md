# EasyAPI Skill

让 agent（Claude Code / ZCode / Codex / Cursor 等）直接调用 EasyAPI 模型网关生成图片、生成视频、
文本对话，并查询可用模型、模型参数与价格。

**核心价值：把「拼接口参数」从模型手里拿走。** 发送前按模型的参数定义逐项校验
（必填、枚举、上下限），参数错误在本地就被拦下并给出精确提示，而不是等上游返回一个含糊的 400。

## 安装

**skill 内容只有一个源**：`plugins/easyapi/skills/easyapi/`。仓库根的 `skills/easyapi/`
是由 `tools/sync_dist.py` 从它生成的副本（标准 `skills/<name>` 布局，给通用安装器用），
两份一致性由测试和 `sync_dist.py --check` 守住，不要手改。

### 方式一：ZCode 插件市场（推荐 ZCode 用户）

1. 打开 **插件市场 / Plugin Marketplace**
2. **添加 / Add → 添加插件市场 / Add Plugin Marketplace**
3. 粘贴这个目录（**市场根目录，不是插件子目录**）：

   ```
   D:\Assets\DevelopProjects\CompanyProjects\easyapi-skill
   ```

4. 添加成功后进 **个人 / Personal → dev-easyapi-skill → EasyAPI 模型网关**，点 **安装 / Install**
5. 安装后也可在 **设置 → 插件 / Settings → Plugins** 里管理

### 方式二：Agent Skills CLI（跨工具，推荐非 ZCode 用户）

[`npx skills`](https://github.com/vercel-labs/skills) 支持 Claude Code、Codex、Cursor 等
几十种工具，按目标工具自动落到各自的 skills 目录：

```bash
npx skills add <owner>/easyapi-skill/plugins/easyapi/skills/easyapi -a claude-code
npx skills update        # 之后更新
npx skills remove easyapi
```

> 需要 Node 22.20+，但**只在安装时用**，skill 运行时只依赖 Python。

### 方式三：直接拷贝 skill 目录

`skills/easyapi/`（生成的副本）或 `plugins/easyapi/skills/easyapi/`（源）都可以整体复制：

| 工具 | 目录 |
|---|---|
| 通用（推荐） | `~/.agents/skills/easyapi/` |
| Claude Code | `~/.claude/skills/easyapi/` |
| 仅当前项目 | `<项目>/.agents/skills/easyapi/` |

目录名必须是 `easyapi`（skill 的 `name` 要与目录名一致）。Windows 上 `~` 是 `%USERPROFILE%`。
复制后 skill 目录是自包含的（脚本按自身位置定位快照，状态写到 `~/.easyapi`），
换个 cwd 也能直接用。

## 配置 API Key

对 agent 说「帮我配置 key」即可——agent 会直接运行浏览器授权（`login --browser`），
你只需要在弹出的网页里点一下「授权」。脚本会自动创建或复用一个名为 `easyapi-skill`
的 API Key，写入 `~/.easyapi/config.json`（权限 600）并查余额验证。全程不需要在
终端输入任何东西，也不需要在控制台手动建 key。

也可以自己动手：

| 命令 | 说明 |
|---|---|
| `login --browser` | **推荐**。浏览器授权，不输密码；Bash 超时建议给足 6 分钟 |
| `login --browser --manual` | 跨机器 / 无默认浏览器：打印授权地址，把页面显示的授权码粘贴回来 |
| `login` | 账号密码登录（两步验证账号会提示输验证码） |
| `login --access-token` | 到控制台 `/setting`「安全设置 → 系统访问令牌」复制令牌 + 用户 ID |
| `login --api-key` | 已有 sk- key，直接粘贴 |
| `EASYAPI_API_KEY` 环境变量 | CI / 脚本化环境 |

细节：

- 密码、系统访问令牌、授权票据**只在内存里用一次，不落盘**；配置文件里只有权限有限的
  sk- key；
- 浏览器授权的回调地址**只允许本机回环**（127.0.0.1 / localhost / ::1），后端强制校验，
  授权码一次性、2 分钟有效；
- 平台开启网页人机验证（Turnstile 等）时密码登录不可用，脚本会提示改用
  `--browser` 或 `--access-token`；
- 自动创建的 key 不限额度、永不过期（额度受账号余额约束），重复登录复用同一个，
  不堆积；随时可在控制台 `/console/token` 改名、限额或删除。

### 设备绑定（新建 key 默认开启）

新建的 key 会绑定**本机设备指纹**：指纹由 Windows `MachineGuid` / Linux `machine-id` /
macOS `IOPlatformUUID` 现算（取不到才退回 MAC + 主机名），**不写进配置文件**。所以：

- 配置文件被拷到别的机器 → key 用不了（`client_binding_mismatch`）；
- 换机、改主机名、还原系统 → 同一台机器也可能变，用 `login --browser --rebind` 换绑：
  新建一个绑定本机的 key，并在落盘后吊销旧 key；
- 复用已有令牌（`--reuse-key`）**不会**改绑定——它可能还在别的脚本/机器上用；
- 要在多台机器用，就分别登录各建一个 key；不想要绑定用 `--no-bind`。

## 快速验证

```bash
E=~/.agents/skills/easyapi/bin/easyapi

"$E" env --network                               # 解释器/目录/网关/接口能力
"$E" quota                                       # key 有效吗？还有额度吗？
"$E" models --category image                     # 有哪些生图模型
"$E" schema seedream-4.5                         # 这个模型有哪些参数、怎么发
"$E" image --model seedream-4.5 --prompt "一只戴墨镜的柴犬"
```

`bin/easyapi` 会自己探测 `python3` → `python` → `py -3`，所以 Windows 上不需要
确认命令名；查询模型和参数**不需要 key**（`/pricing` 与 `/pricing/schema` 都是匿名接口）。

## 命令

| 命令 | 作用 |
|---|---|
| `env [--network]` | 环境自检：解释器、目录、快照、网关与 schema 接口能力 |
| `quota` | 查余额、用量、过期时间 |
| `models [--category image\|video\|text] [--search 关键词]` | 列可用模型 |
| `schema <模型名>` | 参数定义 + 提交端点 + 示例请求体 |
| `compare [模型...] [--category X] [--seconds N] [--resolution R]` | 比价与选型 |
| `recommend --category video --need first-last-frame` | 按能力 + 价格给 3 个候选 |
| `image --prompt "..."` | 生图 |
| `video --prompt "..."` | 生视频（自动轮询到完成并下载） |
| `chat --prompt "..."` | 文本 / 多模态对话 |
| `config` / `login` / `logout` | 配置管理 |

通用开关：`--json`（机器可读输出）、`--refresh`（忽略缓存）、`--dry-run`（只校验并打印请求体，
不发送、**不需要 key**）、`--strict`（定义外的参数直接报错）。

## 生成结果落在哪

图片和视频默认下载到**当前工作目录**的 `easyapi-out/`，文件名形如
`20260921-153012-seedream-4.5-1.png`。用 `--out` 改目录。

脚本会打印**绝对路径**和原始 URL。终端 agent 无法内联渲染图片，给路径最有用。

## 配置与缓存

```
~/.easyapi/
├── config.json          # key、token_id、base_url、默认模型（权限 600）
└── cache/
    ├── pricing.json     # 模型列表（默认缓存 24 小时）
    ├── schemas.json     # 全量参数 schema（/pricing/schema?all=1）
    ├── detail/*.json    # 单模型详情（/pricing/detail）
    └── schema/*.json    # 单模型 schema（/pricing/schema?model_name=）
```

`pricing_version` 变化时，`detail/`、`schema/`、`schemas.json` 会一起作废。

## 安全模型

- key **明文**存在 `~/.easyapi/config.json`（权限位 600；Windows 上 chmod 不生效，
  实际由用户目录 ACL 保护），等价于 `~/.aws/credentials`。也可以只用 `EASYAPI_API_KEY`
  环境变量，优先级更高。
- key **不进命令行参数**（避免 shell history / 进程列表），输出里**不出现任何 key 字符**
  （包括打码片段）。
- 新建 key 默认绑定本机设备指纹；指纹每次现算、不落盘，所以配置泄漏到别的机器无法直接使用。
- 浏览器授权票据一次性、120 秒、仅允许回环回调；登录用的 access_token 只在内存里用一次。
- **防不了什么**：同机恶意软件（能读 key 也能读设备信息）、或用户主动把 key + 指纹一起交出去。
  要限制损失，用 `login --browser --key-quota 20 --key-expire-days 90` 给 key 设上限与有效期。

配置**不在 skill 目录里** —— skill 目录可能只读，插件缓存更新时还会被整体替换。

模型有变动时：`easyapi.py config --clear-cache`，或单次 `--refresh`。

## 网关入口

**接口和控制台是两个域名**，脚本对两者都内置了默认值：

| 用途 | 默认值 | 环境变量 |
|---|---|---|
| 接口（`/v1/...`、`/pricing`、`/usage/token`） | `https://token.easyapi.com` | `EASYAPI_BASE_URL` |
| 控制台前端（浏览器授权页所在） | `https://www.easyapi.com` | `EASYAPI_CONSOLE_BASE_URL` |

私有化部署改这两个值即可：

```bash
easyapi.py config --base-url https://your-gateway.example.com
easyapi.py config --console-base https://your-console.example.com
```

两个域名不能混用：授权页 `/cli/authorize` 是**前端路由**（在控制台域），而票据兑换
`POST /cli/authorize`、`POST /cli/exchange` 是**接口**（在网关域），路径同名但域名不同。
拿网关域名去开页面会打到只接受 POST 的接口上（GET 返回 404），脚本会提前探测并报错。

只有当定价接口被单独拆到另一个域名时，才需要额外设置 `--pricing-base`（不设就跟随网关入口）。

## 环境要求

**Python 3.7+，仅用标准库**，不需要 pip install。`bin/easyapi` 会自动探测
`python3` / `python` / `py -3`，Windows 上装哪个都能用；macOS 和大多数 Linux 自带。

没有 Python 也能用：按 `plugins/easyapi/skills/easyapi/references/fallback-curl.md`
的固定配方手工调用。降级路径**不做本地参数校验**，务必先查 schema 再发送。

## 已知限制

- **参数校验的覆盖范围取决于数据。** 参数按四级来源解析并逐字段合并：
  `/pricing/schema?all=1` → 单模型 `/pricing/schema` → `/pricing/detail` → 内置快照。
  新接口可用时新模型上线即自动生效；四者都没有的模型会跳过校验并明确告知。
  **在管理端补齐模型的 `params` 可以继续提升覆盖率，补完无需重新分发 skill。**
- **只有模型声明过的动态字段才会被网关转发。** 一等字段（`prompt`/`duration`/
  `resolution`/`size`/`aspect_ratio`/`generate_audio`/`seed`/`image`/`images`/
  `input_references`）不受影响；`negative_prompt`、`cfg_scale` 这类必须模型显式声明。
- **只用 API Key 查不到消费明细。** `/user/self`、`/log/self`、`/data/self` 走登录会话鉴权，
  不是 API Key 鉴权。额度可以查（`/usage/token`），明细请到控制台看。
- **按量计费模型无法本地估算价格**，只能给倍率做横向参考。
- **预估价格不含折扣**，仅用于横向比较；实际金额以平台扣费为准。
- **图片异步任务（`/v1/images/tasks`）未接入**，脚本只用同步的 generations/edits。
- **设备绑定只防「配置被拷走」**：同机进程仍能读 key 并算出同一指纹；换机/改主机名需要
  `login --rebind`。需要多机并发就各建一个 key。

## 目录结构

```
easyapi-skill/                              ← 市场根目录（ZCode 添加市场时粘贴这个）
├── marketplace.json                        ZCode 市场清单
├── skills/easyapi/                         生成的标准布局副本（别手改）
├── plugins/
│   └── easyapi/                            插件本体
│       ├── .zcode-plugin/plugin.json       插件清单
│       └── skills/
│           └── easyapi/                    ← skill 源（唯一手改处）
│               ├── SKILL.md
│               ├── bin/easyapi             启动器：探测 python3/python/py -3 + 解析目录
│               ├── references/             按需阅读的细节文档
│               │   ├── image.md / video.md / chat.md
│               │   ├── models.md           计费与比价陷阱
│               │   ├── params-conventions.md  统一参数命名与首尾帧
│               │   ├── account.md          key 与额度
│               │   ├── troubleshooting.md
│               │   ├── fallback-curl.md    无 Python 时的 curl 配方
│               │   └── model-snapshot.json 参数快照（38 个图片/视频模型）
│               └── scripts/easyapi.py      仅标准库
├── tools/build_snapshot.py                 重新生成参数快照（--from-api / --docs）
├── tools/sync_dist.py                      生成 / 校验 skills/easyapi 副本
└── tests/test_easyapi.py                   170 个用例（含打包、重定位、分发漂移、绑定）
```

插件规范要求技能放在 `skills/<name>/SKILL.md`，所以源在插件目录里；仓库根的
`skills/easyapi/` 是为通用安装器生成的副本，两者一致性由 `sync_dist.py --check` 与测试守住。

## 开发

```bash
# 跑测试（纯逻辑 + 本地 mock server + 打包/重定位/分发契约，不需要 key、不碰真实接口）
python3 tests/test_easyapi.py

# 从线上重新生成参数快照（推荐；需要网关已部署 /pricing/schema）
python3 tools/build_snapshot.py --from-api https://token.easyapi.com \
  --out plugins/easyapi/skills/easyapi/references/model-snapshot.json

# 离线：从管理端导出生成
python3 tools/build_snapshot.py --docs ../easyapi-ai-api/docs \
  --out plugins/easyapi/skills/easyapi/references/model-snapshot.json

# 改完 skill 后同步分发副本，并检查是否有漂移
python3 tools/sync_dist.py
python3 tools/sync_dist.py --check
```

参数快照从 `easyapi-ai-api/docs/{image,video,all}_models.json` 生成，会剥掉
`upstream_name` / `source` / `confidence` 三个服务端专有字段（与后端
`model.SanitizeParamsForClient` 口径一致）。

### 改了 skill 内容后怎么让 ZCode 更新

源码改动不是热重载，要走三步：

1. 同步分发副本：`python3 tools/sync_dist.py`（否则根目录 `skills/easyapi/` 会落后）
2. 把 `plugins/easyapi/.zcode-plugin/plugin.json` 的 `version` 加一，
   **同时**把 `marketplace.json` 里对应条目的 `version` 改成一样（版本不一致更新不会生效）。
3. 在 ZCode 里：插件市场页齿轮 → **市场源 / Marketplace Sources** → 找到
   `dev-easyapi-skill` → 点 **刷新该市场**；再回 **个人** → 插件详情 → **更新**。
