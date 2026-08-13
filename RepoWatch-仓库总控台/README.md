# RepoWatch 1.0 · 仓库总控台（Windows 版）

扫描本机所有 Git 仓库，一站式查看 **分支 / 脏文件 / 领先落后 / 最后提交**，支持 **一键打开**（编辑器 / 终端 / 目录）与 **一键快照**（git add → commit → push）。

- **零依赖**：Python 3 标准库；前端无构建、无 CDN。
- **只绑定 127.0.0.1**，写操作校验 HttpOnly 会话 Cookie，勿对外暴露。

## 快速开始

1. 双击 `启动控制台.bat`：后台静默启动（pythonw），自动打开浏览器，端口 9610–9619。
2. 双击 `停止控制台.bat` 停止服务。
3. 命令行：`python repo_watch.py`（前台调试）、`python repo_watch.py --no-browser`。

## 功能

- **仓库总览**：卡片展示仓库名、当前分支、变更数（暂存/修改/新增）、领先/落后、最后提交与远程地址。
- **一键打开**：编辑器（默认 `code`，可在配置中改）、Windows Terminal、资源管理器。
- **一键快照**：单仓库或「全部快照」，执行 `git add -A` → 有变更才提交（默认信息 `chore: snapshot 时间`）→ 有上游才推送，逐步返回结果。
- **扫描范围**：页面内添加/移除根目录，保存后自动重扫；默认扫描桌面、文档及常见开发盘目录（最深 6 层，跳过 node_modules 等）。
- **自动发现**：每 60 秒后台重扫，新仓库自动出现；页面每 15 秒刷新列表。

## 数据位置

- 配置：`data\config.json`（roots / editor_command / terminal_command / rescan_seconds）
- 日志：`data\logs\console.log`；运行标记：`data\server.pid`

## 安全边界

- 服务只监听 `127.0.0.1` 并校验 Host/Origin；写操作需要 HttpOnly 会话 Cookie。
- 快照会以当前用户权限执行 git 命令，只会作用于已扫描到的仓库目录。
