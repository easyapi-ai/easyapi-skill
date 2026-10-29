# 错误排查

脚本会把网关错误翻译成可执行建议。下面是完整对照表。

## 按 HTTP 状态

| 状态 | 含义 | 处理 |
|---|---|---|
| `401` | Key 无效 / 未提供 | 跑 `"$EASYAPI_SKILL_DIR/bin/easyapi" quota` 自检；或重新 `login` |
| `402` | 额度不足 | 引导充值；`"$EASYAPI_SKILL_DIR/bin/easyapi" quota` 看余额 |
| `403` | 无权访问该模型 | 令牌未开通该模型或分组不匹配；`"$EASYAPI_SKILL_DIR/bin/easyapi" models` 看可见模型 |
| `403` + `client_binding_mismatch` | key 绑定在另一台设备上 | `login --browser --rebind` 换绑（新建并吊销旧 key），不要重试或换着试 |
| `404` | 模型不存在或对当前分组不可见 | `"$EASYAPI_SKILL_DIR/bin/easyapi" models` 确认模型名；注意大小写（`Kling-3.0` 不是 `kling-3.0`） |
| `429` | 限流 | 稍后重试 |
| `400` / `422` | 参数被上游拒绝 | 见下 |
| `5xx` | 网关或上游异常 | 稍后重试 |

## 400 参数错误的常见原因

按出现频率排序：

1. **`model is required`** —— 请求体没带 `model` 字段。用 `--model` 指定，或
   `"$EASYAPI_SKILL_DIR/bin/easyapi" config --default-image-model X` 设默认值。

2. **模型名写错或大小写不符** —— 线上模型名大小写敏感，如 `Kling-3.0`、`seedream-4.5`。
   用 `"$EASYAPI_SKILL_DIR/bin/easyapi" models` 复制准确名字。

3. **尺寸/宽高比格式错** —— 全角 `×`、`*`、大写 `X` 都会被拒。
   脚本会自动归一，但如果你绕过脚本手工拼 JSON 就会踩到。

4. **首尾帧参数用错** —— 顶层 `first_frame_url` / `last_frame_file_id` 会被静默丢弃，
   必须用 `input_references`。而且**尾帧不能单独出现**。见
   [params-conventions.md](params-conventions.md)。

5. **枚举值不在允许集合内** —— 例如 `resolution=4k` 而模型只支持 `720P`/`1080P`。
   `"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型>` 看合法取值。

6. **数值越界** —— 如 `duration=30` 而模型上限是 15。

7. **参数名用了厂商原名** —— 例如用了 `aspectRatio` 而不是 `aspect_ratio`。
   网关做参数名映射，客户端要用统一名。

## 参数校验没生效？

如果脚本报了「线上与内置快照都没有 XXX 的参数定义，已跳过参数预校验」，
说明这个模型既没在管理端配 `params`，也不在内置快照里，所以**无法在本地拦截参数错误**。

这不是脚本坏了，是数据缺失。两个办法：

1. 到管理端给该模型补齐 `params`（推荐，补完 skill 自动生效，无需更新分发）。
2. 如果是新模型，用 `"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型> --refresh` 确认线上确实没有。

## 参数到底来自哪一级？

`"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型>` 的第一个字段就是来源：

| 输出 | 含义 |
|---|---|
| `线上 /pricing/schema（全量参数包）` | 新接口可用，参数最新；新模型上线自动生效 |
| `线上 /pricing/schema（单模型）` | 只有单模型接口的部署 |
| `线上 /pricing/detail` | 更老的部署，只有详情接口带 params |
| `内置快照（日期）` | 完全离线兜底，日期是快照生成时间 |

带 `+ 内置快照补全缺失字段` 时，说明线上只声明了一部分参数，其余来自快照。
**这些字段只保证取值形状对，未声明的动态字段可能被网关丢弃**——报错时以线上声明的为准。

用 `"$EASYAPI_SKILL_DIR/bin/easyapi" env --network` 可以确认接口能力：
`schema 接口不可用` 说明网关还没部署新接口，功能会走 3/4 级回退，不影响生成。

## 报「没有找到 Python 解释器」

启动器按 `python3 → python → py -3` 探测，全都没有时会给出这条提示。

- 装 Python 3.7+ 后重试（只用标准库，不需要 pip）；
- 临时降级：按 [fallback-curl.md](fallback-curl.md) 手工调用。降级不做本地校验，
  必须**先查 schema 再发请求**。

## 比价显示「该分辨率没有按秒倍率」

线上各模型的分辨率键写法不统一（`720p` 与 `720` 都有）。换一个 `--resolution` 值试试，
或直接看该模型支持哪些键：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" models --category video --json | grep -A3 video_per_second_ratios
```

## 视频任务卡住 / 超时

超时错误里会带任务 ID 和查询地址，可以手工续查：

```bash
curl -H "Authorization: Bearer $EASYAPI_API_KEY" \
  https://token.easyapi.com/v1/videos/<task_id>
```

也可以加大等待时间：`--timeout 1800`。

任务在服务端是持久化的，本地超时不等于任务失败。

## 报 `No static resource ...`

**入口地址配错了。** 这个响应来自一个普通 Java 服务，不是网关。

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" config                     # 看「网关入口」是什么
"$EASYAPI_SKILL_DIR/bin/easyapi" config --base-url https://token.easyapi.com
```

默认值本来就是 `https://token.easyapi.com`，所以出现这个错误通常是被
`EASYAPI_BASE_URL` 环境变量或配置文件里的 `base_url` 覆盖成了别的地址。
注意 `api.easyapi.com` **不是**网关地址。

## 缓存导致看到的模型不对

模型列表和参数默认缓存 24 小时。刚在管理端加了模型或改了参数时：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" models --refresh        # 只刷新这一次
"$EASYAPI_SKILL_DIR/bin/easyapi" config --clear-cache    # 清空全部缓存
```

缓存会用响应里的 `pricing_version` 做契约变更失效 —— 版本变了会自动作废单模型参数缓存。

## 拿到 traceback 而不是结构化错误

正常情况下不会发生。如果出现了，说明是脚本自身的 bug，把输出里的最后一行反馈给维护者。
设 `EASYAPI_DEBUG=1` 会打印完整堆栈。

## 自查清单

按顺序跑一遍，能定位绝大多数问题：

```bash
"$EASYAPI_SKILL_DIR/bin/easyapi" env --network              # 0. 解释器/目录/网关/schema 接口
"$EASYAPI_SKILL_DIR/bin/easyapi" config                    # 1. key 配了吗？两个 base_url 对吗？
"$EASYAPI_SKILL_DIR/bin/easyapi" quota                     # 2. key 有效吗？还有额度吗？
"$EASYAPI_SKILL_DIR/bin/easyapi" models --category image   # 3. 模型名对不对？当前分组能看到吗？
"$EASYAPI_SKILL_DIR/bin/easyapi" schema <模型名>            # 4. 参数名与取值对吗？schema 从哪来的？
"$EASYAPI_SKILL_DIR/bin/easyapi" image --model <模型名> --prompt "test" --dry-run   # 5. 请求体长什么样？
```
