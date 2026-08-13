# localTools — 本地开发工具集

Windows 下配合 vibe coding 使用的自研本地工具集。统一风格：**Python 3 标准库零依赖**、**只监听 127.0.0.1**、**bat 双击启动**、中文界面。

## 工具清单

| 工具 | 用途 | 入口 |
| --- | --- | --- |
| PortWatch-端口监控 | 本地服务总控台：启动台 / 进程监控 / 端口发现 / 日志中心 | `启动控制台.bat` |
| TokenWatch-Token用量统计 | 跨工具 Token 用量统计看板（Codex / Kimi Code / OpenClaw） | `打开Token用量看板.bat` |
| RepoWatch-仓库总控台 | 扫描本机 Git 仓库：分支 / 脏文件 / 最后提交，一键打开与快照提交 | `启动控制台.bat` |
| EnvWatch-环境体检台 | 汇总 python/node/uv/git 等版本、路径冲突、PATH 问题，生成体检报告 | `生成体检报告.bat` |
| CacheWatch-磁盘瘦身 | 可视化 node_modules / .git / __pycache__ / 各类缓存占用，安全一键清理 | `启动控制台.bat` |
| CostWatch-成本核算 | 基于 TokenWatch 数据库 + 模型价格表，按工具/模型/日期折算 USD 成本 | `启动控制台.bat` |
| GitMate-GitHub助手 | GitHub 建仓并推送代码、图形化分支管理、我的仓库一键克隆 | `启动控制台.bat` |

## 通用约定

- 所有 Web 工具只绑定 `127.0.0.1`，写操作校验 HttpOnly 会话 Cookie，勿对外暴露。
- 端口分段：PortWatch 9600–9609，RepoWatch 9610–9619，EnvWatch 9620–9629，CacheWatch 9630–9639，CostWatch 9640–9649，GitMate 9650–9659。
- 数据目录默认放各自工具文件夹 `data\`（已 gitignore），全局共享数据在 `~\.token-watch\`。
