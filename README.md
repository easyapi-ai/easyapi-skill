# EasyAPI Skill

让你的 AI 直接调用 EasyAPI 生成图片和视频。

## 一句话安装

把这句话发给你的 AI（Claude Code、ZCode、Codex、Cursor 都行）：

```
帮我安装 https://github.com/easyapi-ai/easyapi-skill 这个技能
```

它会自己拉仓库、装好两个技能，并带你登录一次。

国内拉不动就换成 Gitee 地址：`https://gitee.com/easyapi-ai/easyapi-skill`

## 装完先登录

新开一个会话，发送：

```
/easyapi-auth
```

AI 会打开授权页，你点一下「授权」即可。凭据存到 `~/.easyapi/config.json`，配置成功后它会告诉你账号、令牌名和余额。

已经登录过就不用再来一次，直接进下一步。

## 开始用

```
/easyapi-studio 画一张赛博朋克雨夜街景，16:9
```

```
/easyapi-studio 做一个猫在草地上跑的 5 秒视频
```

```
/easyapi-studio 查一下 EasyAPI 上有哪些生图模型，分别多少钱
```

不写模型也能直接用：**图片默认 `qwen-image-3.0-pro`，视频默认 `wan3.0-video`**。
生成图片最多等 30 分钟、视频最多等 15 分钟，脚本会自己等到完成并把文件下载到本地。

## 默认模型与参数

- **换默认模型**：跟 AI 说「以后图片默认用 seedream-4.5」即可（它会执行
  `config --default-image-model seedream-4.5`）；视频同理用 `--default-video-model`。
  传空字符串恢复内置默认。
- **只想换这一次**：直接在对话里点名模型，例如「用 Kling-3.0 做一个 10 秒视频」。
- **没给参数时**：像「画一只猫」这种简单需求直接按默认参数出图，不追问；
  只有做封面、壁纸、系列图、多平台尺寸这类**有明确用途、要复用**的任务，
  AI 才会先问 1~3 个关键选项（画幅、时长、分辨率），你说「随便」就按默认值发。
- **结果预览**：支持在对话里显示图片/视频的 agent 会直接展示效果，有预览面板的会主动打开；
  否则给本地文件链接，点开就能看。

## 两个技能

- `easyapi-auth`：登录、换 key、查余额
- `easyapi-studio`：生成图片与视频、对话、查模型与价格

两者共用 `~/.easyapi/config.json`，装一个插件两个都有。

## 手动安装（三种，选一种）

- ZCode：插件市场 → 添加市场 → 粘贴仓库地址 → 安装 EasyAPI。
- 终端：`npx skills add easyapi-ai/easyapi-skill`（需 Node ≥ 22.20 与 git）。
- 手动：下载 zip，把 `easyapi-auth/`、`easyapi-studio/` 复制到 `~/.claude/skills/` 或 `~/.agents/skills/`。

## 要求

Python 3.8+，只用标准库，不需要 pip。状态放在 `~/.easyapi/`（可用 `EASYAPI_HOME` 覆盖）。

## 更多

- 参数与调用细节：`plugins/easyapi/skills/easyapi-studio/references/`
- 没有 Python 时的 curl 兜底：`references/fallback-curl.md`
- 问题反馈：GitHub / Gitee Issues
