# EasyAPI Skill

让 agent（Claude Code / ZCode / Codex / Cursor）直接调用 EasyAPI 模型网关：生成图片、生成视频、文本与多模态对话，查询模型列表、参数定义与价格。

**把「拼接口参数」从模型手里拿走。** 发送前按模型声明的参数逐项校验（必填、枚举、上下限），错误在本地就被拦下；异步视频任务由脚本自己轮询到完成并下载。

主仓 <https://github.com/easyapi-ai/easyapi-skill> · 镜像 <https://gitee.com/easyapi-ai/easyapi-skill> · MIT

## 两个技能，一次装齐

| 技能 | 职责 |
|---|---|
| `easyapi-auth` | 登录与凭据：浏览器授权、自动创建或复用令牌、设备绑定、查余额 |
| `easyapi-studio` | 生成与查询：图片 / 视频 / 对话，模型列表、参数、比价 |

**两个都必要，任何一种安装方式都会同时装到两个**（它们共用同一份配置 `~/.easyapi/config.json`，缺一个就少一半能力）。

## 安装

### 方式一：让 AI 装（推荐）

把下面这段整段发给你的 agent（Claude Code / ZCode / Codex / Cursor 都行）：

```text
请帮我安装 EasyAPI Skill。它有两个技能，都要装：easyapi-auth 负责登录，
easyapi-studio 负责生成图片/视频和查询模型。

1. 把 https://github.com/easyapi-ai/easyapi-skill 克隆到临时目录
   （国内用 https://gitee.com/easyapi-ai/easyapi-skill；没有 git 就下载 zip 解压）
2. 把 plugins/easyapi/skills/easyapi-auth 和 plugins/easyapi/skills/easyapi-studio
   两个目录整体复制到你的 skills 目录（Claude Code 用 ~/.claude/skills/，
   其它工具用 ~/.agents/skills/），目录名保持不变
3. 运行 <skills目录>/easyapi-auth/bin/easyapi-auth env --network 自检
   （需要 Python 3.7+，不需要 pip；Windows 上用 Git Bash）
4. 自检通过后运行 <skills目录>/easyapi-auth/bin/easyapi-auth login --browser，
   把授权页链接发给我
5. 完成后告诉我配置写在哪、账户余额，并给我 3 个可以直接说的话术示例
```

### 方式二：ZCode 插件市场

1. 插件市场 → **添加 / Add → 添加插件市场**
2. 粘贴仓库地址（**仓库根，不是插件子目录**）：

   ```text
   国内：https://gitee.com/easyapi-ai/easyapi-skill.git
   海外：https://github.com/easyapi-ai/easyapi-skill.git
   ```

3. 进入 **个人 → easyapi-skill → EasyAPI**，点 **安装**
4. 之后在 **设置 → 插件** 里管理

### 方式三：Agent Skills CLI

需要 **Node ≥ 22.20 + git**（只在安装时用，运行时只需要 Python）。默认装到当前项目，加 `-g` 装到用户级：

```bash
npx skills add easyapi-ai/easyapi-skill -g
npx skills add https://gitee.com/easyapi-ai/easyapi-skill.git -g   # 国内
npx skills update
npx skills remove easyapi-auth easyapi-studio
```

### 方式四：手动复制

下载 zip 或克隆仓库，把 `easyapi-auth/` 和 `easyapi-studio/` 两个目录整体复制到：

| 工具 | 目录 |
|---|---|
| 通用（推荐） | `~/.agents/skills/` |
| Claude Code | `~/.claude/skills/` |
| 仅当前项目 | `<项目>/.agents/skills/` |

目录名必须与技能名一致；Windows 上 `~` 是 `%USERPROFILE%`。

## 配置 API Key

装好后直接对 agent 说 **「帮我配置 key」**：它会打开浏览器授权页，你点一下「授权」即可，脚本自动创建或复用一个名为 `easyapi-skill` 的 key 写入 `~/.easyapi/config.json`，并查余额验证。不需要在终端输入任何东西，也不用去控制台手动建 key。

<details>
<summary>其他登录方式</summary>

| 命令 | 说明 |
|---|---|
| `login --browser` | **推荐**，浏览器授权，不输密码；授权后页面不跳转，脚本自己取票 |
| `login --browser --manual` | 跨机器/无默认浏览器：打印授权地址，把授权码粘贴回来 |
| `login --browser --loopback` | 旧流程（本机回调），只在后端/网页版本过旧时需要 |
| `login` | 账号密码（开启人机验证时不可用） |
| `login --access-token` | 控制台「安全设置 → 系统访问令牌」+ 用户 ID |
| `login --api-key` | 已有 `sk-` key，直接粘贴 |
| `EASYAPI_API_KEY` | 环境变量，优先级高于配置文件（CI 用） |

- 密码、访问令牌、授权票据只在内存用一次，不落盘；配置文件里只有权限有限的 `sk-` key。
- 新建 key 默认绑定本机设备指纹（现算、不落盘）。换机或改主机名用 `login --browser --rebind` 换绑；不想绑定用 `--no-bind`。
- key 默认不限额度、永不过期，可在控制台 `/console/token` 随时限额或删除；也可 `login --browser --key-quota 20 --key-expire-days 90` 建一个带上限的。

</details>

## 开始用

装好并登录后，直接用自然语言说：

- 「帮我生成一只戴墨镜的柴犬，1024x1024」
- 「做一个猫在草地上跑的 5 秒视频」
- 「EasyAPI 上有哪些生图模型，分别多少钱」

## 命令

| 命令 | 作用 |
|---|---|
| `env --network` | 环境自检：解释器、目录、网关与 schema 接口能力 |
| `quota` | 查余额与用量 |
| `models --category image` | 列可用模型 |
| `schema <模型名>` | 参数定义 + 提交端点 + 示例请求体 |
| `compare` / `recommend` | 比价与选型 |
| `image` / `video` / `chat` | 生成图片 / 视频 / 对话 |
| `config` / `login` / `logout` | 配置管理 |

通用开关：`--json`、`--refresh`（忽略缓存）、`--dry-run`（只校验并打印请求体，不发送、不需要 key）、`--strict`（定义外参数直接报错）。

## 环境要求

**Python 3.7+，仅用标准库**，不需要 `pip install`。启动器会自动探测 `python3` / `python` / `py -3`。

没有 Python 时按 [`fallback-curl.md`](plugins/easyapi/skills/easyapi-studio/references/fallback-curl.md) 的固定配方手工调用——降级路径**不做参数校验**，务必先查 schema 再发送。

## 文件落在哪

- 生成结果：当前目录下的 `easyapi-out/`（用 `--out` 改），脚本打印绝对路径和原始 URL。
- 配置与缓存：`~/.easyapi/`（`config.json` 权限 600；`cache/` 缓存模型列表与 schema，24 小时过期）。设 `EASYAPI_HOME` 可整体换目录。

## 安全边界

- key 明文存在 `~/.easyapi/config.json`，等价于 `~/.aws/credentials`；不进命令行参数，输出里不出现任何 key 字符（含打码片段）。
- 设备指纹只在有 `Authorization` 的请求里发送，且每次现算、不落盘；配置被拷到别的机器无法直接使用。
- **防不了**同机恶意软件，也防不了用户主动把 key 和设备信息一起交出去。

<details>
<summary>已知限制与网关域名</summary>

- 参数校验覆盖取决于数据：按 `/pricing/schema?all=1` → 单模型 schema → `/pricing/detail` → 内置快照四级合并；都没有的模型会跳过校验并明确告知。
- 只有模型**声明过**的动态字段才会被网关转发；`negative_prompt`、`cfg_scale` 这类需要模型显式声明。
- 只用 API Key 查不到消费明细（明细走登录会话），额度可以查。
- 预估价格不含折扣，仅用于横向比较。
- 接口域名 `https://token.easyapi.com`（`EASYAPI_BASE_URL`），控制台域名 `https://www.easyapi.com`（`EASYAPI_CONSOLE_BASE_URL`）。两个域名不能混用：授权页是前端路由，票据兑换是接口，路径同名。

</details>

## 开发

```bash
python3 tests/test_easyapi.py        # 纯逻辑 + mock server，不需要 key
python3 tools/sync_dist.py           # 改完 skill 后同步分发副本
python3 tools/sync_dist.py --check   # 校验副本无漂移
```

<details>
<summary>改了 skill 内容后如何让 ZCode 更新</summary>

1. `python3 tools/sync_dist.py`
2. `plugins/easyapi/.zcode-plugin/plugin.json` 与 `marketplace.json` 的 `version` **同时**加一（不一致更新不生效）
3. ZCode：插件市场 → 齿轮 → 市场源 → 刷新该市场 → 个人 → 插件详情 → 更新

</details>

## 反馈

GitHub 主仓提 issue / PR（响应更快），Gitee 侧合并后同步。
