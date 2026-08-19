# TokenWatch — 跨工具 Token 用量自动统计

自动扫描本机 AI 编程工具的会话记录，汇总 **Token 消耗** 与 **所用模型**，支持 Codex / Kimi Code / OpenClaw / DeepSeek Harness（DSH）四类工具。

## 功能

- 自动扫描三类工具的本地会话记录，按内容去重入库（SQLite）
- 桌面一键启动：双击 `打开Token用量看板.bat` 先增量扫描，再自动打开看板
- 看板打开后**持续自动更新**：后台每 2 分钟扫描一次并刷新看板，页面每 5 分钟自动重新加载，不用手动重复运行
- 结束后台自动刷新：双击 `关闭TokenWatch.bat`
- 生成 HTML 看板：统计卡片（**Token 总计 / 近 30 天 / 近 7 天** / 输入 / 输出 / 记录条数）、30 天每日用量堆叠柱状图、工具占比环形图、Top 模型排行、最近用量明细
- 命令行报表：按工具 / 按模型 / 按日汇总

## 数据来源

| 工具 | 路径 | 用量字段 |
| --- | --- | --- |
| Codex | `~/.codex/sessions/` + `archived_sessions/` | `token_count` 事件（input / cached / output / reasoning） |
| Kimi Code | `~/.kimi-code/sessions/**/agents/main/wire.jsonl` | `usage.record` 事件（inputOther / inputCacheRead / inputCacheCreation / output） |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | assistant 消息上的 `usage` 对象（含 cost.total） |
| DeepSeek Harness | ① `~/.dsh/storages/**/session_projcache.json`（优先，每会话汇总）② `~/.dsh/sessions/**/session.jsonl` 或 `session.jsonl.zstd`（无汇总时回退） | ① `tokenUsage.totals`（uncachedInputTokens / cacheReadTokens / cacheWriteTokens / outputTokens）② `assistant/message` 与 `assistant/chunk` 的 `usage` 事件。模型名一律从各会话日志的 `request/header`（`header.config.model`）提取，按 session uuid 回填到 projcache 汇总。需 `pip install zstandard` 才能读 .zstd 日志（缺失时自动退回系统 `zstd` 命令并记日志） |

## 使用（桌面版，推荐）

工具放在桌面 **`TokenWatch-Token用量统计`** 文件夹，**双击 `打开Token用量看板.bat`** 即可：先增量扫描最新用量并生成看板，再自动打开统计看板；同时会在后台启动一个每 2 分钟扫描一次的更新器（隐藏运行），看板页面每 5 分钟自动刷新，所以**开着看板就能看到持续更新的数据**。

**不用看了**：双击 `关闭TokenWatch.bat` 可结束后台更新器（不影响已入库的数据）。

需要看终端汇总报表时，在 PowerShell 中运行：

```powershell
python "$HOME\Desktop\TokenWatch-Token用量统计\token_watch.py" report
```

> 数据统一存放在 `~/.token-watch/token_watch.db`，历史记录不会因移动文件夹而丢失，也不影响开机速度。
> 可选：如需后台每 10 分钟自动扫描（不开机自启，仅任务计划），可用管理员 PowerShell 运行 `python "$HOME\Desktop\TokenWatch-Token用量统计\token_watch.py" install`，随时可用 `uninstall` 取消。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `scan [--force] [--quiet]` | 增量扫描入库（计划任务调用此命令） |
| `report [--days 30] [--json]` | 终端汇总：工具 × 模型 × 每日用量 |
| `dashboard` | 生成 HTML 看板 |
| `install` / `uninstall` | 可选：注册 / 删除后台每 10 分钟自动扫描的计划任务 |
| `watch --interval 120 --dashboard 看板路径` | 循环扫描并同步刷新看板（打开看板时自动启动） |

## 文件位置

- 桌面工具（推荐入口）：`~/Desktop/TokenWatch-Token用量统计/`（`打开Token用量看板.bat`、`token_watch.py`、`dashboard.html`）
- 数据库：`~/.token-watch/token_watch.db`
- 看板：`~/.token-watch/dashboard.html`
- 扫描日志：`~/.token-watch/scan.log`

## 卸载

```powershell
python "$HOME\.token-watch\token_watch.py" uninstall
```

> 提示：本工具只读取本地会话记录，不会上传任何数据；Token 统计口径为各工具上报的 usage 字段之和。