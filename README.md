# AppliedWebTerminal AstrBot 插件

适用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的基于 [Applied Web Terminal](https://www.curseforge.com/minecraft/mc-mods/applied-web-terminal) 的群监控插件，支持 QQ（OneBot）等平台。

## 功能

- **终端绑定**：一键绑定多个 AE2 Web Terminal，集中管理
- **实时状态查询**：随时查看终端合成进度与 CPU 占用情况
- **CPU 详情查看**：可查看指定合成 CPU 内部的详细合成条目，效果接近 Web 页面
- **合成完成提醒**：后台自动轮询，合成完成后主动推送提醒
- **图片/文本双模式**：状态查询支持精美的图片展示或简洁的文本输出
- **灵活订阅**：可按需订阅特定终端的全部 CPU 或指定 CPU 编号

## 命令列表

| 命令 | 说明 |
|------|------|
| `/ae terminals` | 查看可绑定的终端列表 |
| `/ae bind <终端UUID或唯一前缀> <密码>` | 绑定终端（默认订阅全部 CPU 完成提醒） |
| `/ae unbind <终端UUID或唯一前缀>` | 解绑终端 |
| `/ae list` | 查看当前群已绑定的终端及订阅状态 |
| `/ae status` | 查看合成状态（默认图片模式） |
| `/ae status text` | 以文本格式查看状态 |
| `/ae status image` | 以图片格式查看状态 |
| `/ae status busy` | 仅查看正在有合成任务的 CPU |
| `/ae status <终端UUID或唯一前缀>` | 查看指定终端的状态 |
| `/ae status <终端UUID或唯一前缀> busy` | 查看指定终端的忙碌 CPU |
| `/ae statusimg [终端UUID或唯一前缀]` | 强制以图片格式查看忙碌 CPU |
| `/ae cpu <编号>` | 查看单个 CPU 的详细合成情况 |
| `/ae cpu <编号> text` | 以文本模式查看单个 CPU 详情 |
| `/ae cpu <终端UUID或唯一前缀> <编号>` | 查看指定终端下某个 CPU 的详细合成情况 |
| `/ae cpu <终端UUID或唯一前缀> <编号> text` | 以文本模式查看指定终端 CPU 详情 |
| `/ae watch <终端UUID或唯一前缀> all` | 订阅该终端全部 CPU 完成提醒 |
| `/ae watch <终端UUID或唯一前缀> cpu <编号>` | 订阅指定 CPU 完成提醒 |
| `/ae unwatch <终端UUID或唯一前缀> all` | 取消订阅该终端全部 CPU |
| `/ae unwatch <终端UUID或唯一前缀> cpu <编号>` | 取消订阅指定 CPU |

> UUID 支持唯一前缀匹配。若当前群只绑定了一个终端，`cpu` / `watch` / `unwatch` 命令还可省略 `<终端UUID>`。

## 安装与配置

### 1. 安装依赖

插件依赖 AstrBot 内置的 HTML 转图片服务（Playwright），并使用 `websockets` 拉取 CPU 详细状态。

### 2. 配置插件

在 AstrBot WebUI 的插件管理页面中配置：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `base_url` | 字符串 | AE2 Web Terminal 服务器地址，例如 `http://example.com:5045` |
| `mute_keywords` | 列表 | 合成完成提醒静默关键词，支持正则表达式 |
| `mute_periods` | 列表 | 合成完成提醒静默时间段，格式为 `HH:MM-HH:MM`，支持跨天与多个时间段 |

也可通过环境变量 `AWT_BASE_URL` 设置终端地址（优先级低于 WebUI 配置）。

### 3. 静默规则示例

在 `mute_keywords` 中添加正则表达式，匹配到的产物合成完成时将**不发送提醒**：

- `"星辰石粉"`：匹配任何包含"星辰石粉"的物品名
- `"^火箭燃料$"`：仅匹配精确等于"火箭燃料"的物品名
- `".*粉$"`：匹配所有以"粉"结尾的物品名

在 `mute_periods` 中添加时间段，命中该时间段时将**不发送完成提醒**：

- `"23:00-07:00"`：每天 23:00 到次日 07:00 静默
- `"12:30-13:30"`：每天午休时段静默
- `["23:00-07:00", "12:30-13:30"]`：同时配置多个静默时间段

修改配置后需**重启 AstrBot** 生效。

## 效果预览

- 状态查询：以精美的卡片式图片展示各终端 CPU 的空闲/忙碌状态、合成物品图标、数量及耗时
- CPU 详情：可查看指定 CPU 当前任务、项目进度、剩余项目数及内部条目明细
- 完成提醒：合成完成后自动推送图片提醒，包含物品名称、数量、耗时等信息

## 模板结构

- 页面模板已从 `main.py` 拆分到 `templates/` 目录
- `templates/status.html`：状态总览图
- `templates/cpu_detail.html`：单个 CPU 详情图
- `templates/completion.html`：完成提醒图
