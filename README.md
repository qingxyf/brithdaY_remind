# 生日提醒

一个用于 AstrBot 的生日提醒插件。插件会读取 `data/plugin_data/brithday_remind/birthdays.txt`，每天在配置时间检查当天生日，并向每条记录指定的群聊发送祝福。

本插件使用 `gpt-5.5` 协助开发。

## 功能

- 默认在 `Asia/Shanghai` 时区每天 `00:00` 检查生日。
- 群号不写在代码或默认配置里，由 `birthdays.txt` 每条记录单独指定。
- 支持同一天同一群多个人生日一起祝福。
- 默认调用 AstrBot 当前会话模型，根据提示词生成祝福语，并接入当前 QQ 机器人人设。
- 支持出生年份，自动计算年龄。
- 记录已发送状态，避免重启后同一天对同一群重复发送。
- 主动发送前会尝试检查目标群是否可用；如果机器人不在群或目标不可达，会跳过发送并记录日志，避免调度任务报错中断。

## 运行逻辑

假设 `birthdays.txt` 中有一行：

```txt
张三 2001-05-03 12345678
```

插件启动后会计算下一次检查时间。例如现在是 `2026-05-02 10:00`，配置的 `send_time` 是 `00:00`，下一次检查会安排在 `2026-05-03 00:00`。

到点后，插件会读取 `data/plugin_data/brithday_remind/birthdays.txt`，筛选当天 `MM-DD` 等于 `05-03` 的记录。如果发现张三生日，就提取该记录目标群当前生效的人设 prompt，连同生日信息交给模型生成祝福，并主动发送到这一行配置的群 `12345678`。发送成功后会写入 `data/plugin_data/brithday_remind/sent_state.json`，避免当天重复发送。

它不是为每个人单独注册一个未来定时器，而是插件内部维护一个“每天一次”的检查循环：每次到点检查今天有哪些生日，再按群分组发送。

## 生日文件格式

编辑 `data/plugin_data/brithday_remind/birthdays.txt`，每行一个人物、生日和群号，三列用空格分隔。仓库里的 `birthdays.example.txt` 只作为格式示例：

```txt
张三 2001-05-02 12345678
李四 05-02 23456789
王五 5月2日 34567890
```

支持格式：

- `姓名 YYYY-MM-DD 群号`
- `姓名 YYYY/MM/DD 群号`
- `姓名 YYYY年M月D日 群号`
- `姓名 MM-DD 群号`
- `姓名 M月D日 群号`

群号也可以写完整 AstrBot session，例如：

```txt
张三 2001-05-02 平台ID:GroupMessage:12345678
```

以 `#` 开头的行会被忽略。

## 配置

可在 AstrBot 插件配置页修改：

- `send_time`：每天发送时间，默认 `00:00`。
- `timezone`：时区，默认 `Asia/Shanghai`。
- `platform_type`：平台类型，默认 `aiocqhttp`。
- `platform_id`：平台 ID，默认留空自动探测。
- `use_llm_blessing`：是否使用模型生成祝福，默认开启。
- `llm_provider_id`：指定模型提供商 ID，留空使用目标群会话当前默认聊天模型。
- `llm_model`：指定模型名，留空使用提供商默认模型。
- `llm_system_prompt`：模型系统提示词，会与当前人设和生日规则合并。
- `llm_prompt_template`：祝福生成提示词，可用 `{names}`、`{details}`、`{date}`、`{count}`，默认要求正文出现姓名。
- `use_persona_prompt`：是否接入目标群当前生效的人设 prompt，默认开启。
- `persona_prompt_max_chars`：注入人设最大字符数，默认 `1200`。
- `message_template`：固定祝福模板，仅在关闭模型生成或模型调用失败时使用。

如果主动发送失败，请把 `platform_id` 设置为 `data/cmd_config.json` 中平台配置的 `id`。主动发送 session 会形如：

```txt
平台ID:GroupMessage:群号
```

## 防乱码

- `birthdays.txt` 会依次尝试 `utf-8-sig`、`utf-8`、`gb18030` 读取，减少 Windows 文本编码导致的乱码。
- 如果检测到生日文件或模型输出疑似乱码，会记录错误并回退到固定模板，避免把“看不懂乱码”这类内容发到群里。

## 隐私说明

- 插件不再内置任何默认群号，公开仓库中也不应提交真实群号、机器人平台 ID 或真实群成员生日。
- `birthdays.txt` 是本地数据文件，默认位于 `data/plugin_data/brithday_remind/`，已加入 `.gitignore`；公开仓库只保留 `birthdays.example.txt`，不要提交真实名单。
- 解析错误只返回行号和错误类型，不回显整行内容，避免把真实姓名或群号暴露到聊天里。

## 命令

- `/birthday_check`：查看当前群今天是否有人生日，并预览祝福语。
- `/birthday_reload`：重新读取并检查 `birthdays.txt`。
- `/birthday_send_today`：在当前群手动发送今天的生日祝福，用于群内测试；只会读取当前群对应的生日记录。
- `/birthday_next`：查看当前群最近 5 个生日。

## 注意

- 插件目录名沿用仓库和用户指定路径：`brithday_remind`。
- 远程仓库名沿用用户指定地址：`brithdaY_remind.git`。
