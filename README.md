# AppliedWebTerminal AstrBot 插件

适用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的基于 [Applied Web Terminal](https://www.curseforge.com/minecraft/mc-mods/applied-web-terminal) 的群监控插件，支持 QQ（OneBot）等平台。

## 功能

- **终端绑定**：一键绑定多个 AE2 Web Terminal，集中管理
- **实时状态查询**：随时查看终端合成进度与 CPU 占用情况
- **合成完成提醒**：后台自动轮询，合成完成后主动推送提醒
- **图片/文本双模式**：状态查询支持精美的图片展示或简洁的文本输出
- **灵活订阅**：可按需订阅特定终端的全部 CPU 或指定 CPU 编号

## 命令列表

| 命令 | 说明 |
|------|------|
| `/ae terminals` | 查看可绑定的终端列表 |
| `/ae bind <终端UUID> <密码>` | 绑定终端（默认订阅全部 CPU 完成提醒） |
| `/ae unbind <终端UUID>` | 解绑终端 |
| `/ae list` | 查看当前群已绑定的终端及订阅状态 |
| `/ae status` | 查看合成状态（默认图片模式） |
| `/ae status text` | 以文本格式查看状态 |
| `/ae status image` | 以图片格式查看状态 |
| `/ae status <终端UUID>` | 查看指定终端的状态 |
| `/ae statusimg [终端UUID]` | 强制以图片格式查看状态 |
| `/ae watch <终端UUID> all` | 订阅该终端全部 CPU 完成提醒 |
| `/ae watch <终端UUID> cpu <编号>` | 订阅指定 CPU 完成提醒 |
| `/ae unwatch <终端UUID> all` | 取消订阅该终端全部 CPU |
| `/ae unwatch <终端UUID> cpu <编号>` | 取消订阅指定 CPU |

> 若当前群只绑定了一个终端，`watch`/`unwatch` 命令可省略 `<终端UUID>`，直接使用 `/ae watch all` 即可。

## 安装与配置

### 1. 安装依赖

插件依赖 AstrBot 内置的 HTML 转图片服务（Playwright），无需额外安装 Pillow。

### 2. 配置插件

在 AstrBot WebUI 的插件管理页面中配置：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `base_url` | 字符串 | AE2 Web Terminal 服务器地址，例如 `http://example.com:5045` |
| `mute_keywords` | 列表 | 合成完成提醒静默关键词，支持正则表达式 |

也可通过环境变量 `AWT_BASE_URL` 设置终端地址（优先级低于 WebUI 配置）。

### 3. 静默关键词示例

在 `mute_keywords` 中添加正则表达式，匹配到的产物合成完成时将**不发送提醒**：

- `"星辰石粉"`：匹配任何包含"星辰石粉"的物品名
- `"^火箭燃料$"`：仅匹配精确等于"火箭燃料"的物品名
- `".*粉$"`：匹配所有以"粉"结尾的物品名

修改配置后需**重启 AstrBot** 生效。

## 效果预览

- 状态查询：以精美的卡片式图片展示各终端 CPU 的空闲/忙碌状态、合成物品图标、数量及耗时
- 完成提醒：合成完成后自动推送图片提醒，包含物品名称、数量、耗时等信息
