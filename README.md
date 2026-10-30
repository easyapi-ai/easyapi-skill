# EasyAPI Skill

让你的 AI 直接调用 EasyAPI 生成图片和视频。

## 一句话安装

把这句话发给你的 AI（Claude Code、ZCode、Codex、Cursor 都行）：

```
帮我安装 https://github.com/easyapi-ai/easyapi-skill 这个技能
```

它会自己拉仓库、装好两个技能，并带你登录一次。

国内拉不动就换成 Gitee 地址：`https://gitee.com/easyapi-ai/easyapi-skill`

## 装完能做什么

在对话里直接说：

- 「用 qwen-image-3.0-pro 画一张赛博朋克雨夜街景，16:9」
- 「做一个猫在草地上跑的 5 秒视频」
- 「EasyAPI 上有哪些生图模型，分别多少钱」

发送前按模型参数声明校验，异步任务自动轮询到完成。

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
