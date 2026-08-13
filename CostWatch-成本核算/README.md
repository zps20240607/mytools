# CostWatch 1.0 · 成本核算（Windows 版）

基于 TokenWatch 数据库（`~/.token-watch/token_watch.db`）**只读**查询 Token 用量，结合可编辑的模型价格表，按 **工具 / 模型 / 日期** 折算 **USD 成本估算**，并与工具上报成本（OpenClaw 等）对照。

- **零依赖**：Python 3 标准库；前端无构建、无 CDN。
- **只监听 127.0.0.1**；只读 TokenWatch 数据库，绝不写改；仅价格表可写（需会话 Cookie）。

## 快速开始

1. 双击 `启动控制台.bat`：后台静默启动（pythonw），自动打开浏览器，端口 9640–9649。
2. 双击 `停止控制台.bat` 停止服务。
3. 命令行：`python cost_watch.py report [--days 30] [--json]` 输出终端报表。

## 功能

- **总览卡片**：估算总成本、输入/输出/推理 Token、记录数、上报成本对照。
- **每日成本柱状图**：近 N 天估算成本与 Token（悬停查看明细）。
- **按工具 / 按模型明细**：成本、Token、记录数、匹配到的价格规则与单价。
- **未匹配模型提醒**：列出用默认价估算的模型，方便补充价格规则。
- **价格表编辑**：页面内 JSON 编辑 `data\prices.json`，规则按 pattern 最长优先匹配；内置 GPT / Claude / Gemini / DeepSeek / Kimi / GLM / Qwen 等常用模型默认价（USD/百万 Token，可自行校准）。

## 估算口径

`input×输入价 + output×输出价 + cached×缓存读取价 + cache_write×缓存写入价 + reasoning×输出价`。
默认价与各厂商官网有出入时，请以 `data\prices.json` 为准自行校准；「工具上报成本」列展示 TokenWatch 记录的费用字段（¥），与估算相互对照。

## 数据位置

- 价格表：`data\prices.json`；日志：`data\logs\console.log`；运行标记：`data\server.pid`
- 数据源（只读）：`~/.token-watch/token_watch.db`
