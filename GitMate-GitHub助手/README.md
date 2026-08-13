# GitMate 1.0 · GitHub 图形助手（Windows 版）

面向 git 新手的图形化 GitHub 工具：**创建对应仓库并推送代码** + **图形化分支管理**，全部中文提示、逐步反馈。

- **零依赖**：Python 3 标准库 + GitHub REST API；前端无构建、无 CDN。
- **只监听 127.0.0.1**；写操作要求 HttpOnly 会话 Cookie。
- **凭据安全**：GitHub Token 用 Windows DPAPI（CryptProtectData）加密保存于 `data\secret.bin`，不落明文、不出本机；可随时在「设置」中清除。

## 快速开始

1. 双击 `启动控制台.bat`：后台静默启动（pythonw），自动打开浏览器，端口 9650–9659。
2. 首次使用：粘贴 GitHub Personal Access Token（需要 `repo` 权限，细粒度 Token 需开启 Contents 读写 + Administration 读）。
3. 在「本地仓库」选择仓库 → 分支管理 / 快照 / 推送 / 发布到 GitHub。
4. 双击 `停止控制台.bat` 停止服务。

## 功能

### 发布到 GitHub（创建对应仓库并推送）
- 自动流程：`git init`（如需要）→ GitHub 建仓（可选私有、描述）→ 关联 origin → 快照提交变更 → `push -u origin`。
- 每一步返回结果；仓库重名（422）、已有 origin 等错误给出中文提示；勾选「覆盖已有 origin」可替换旧远程。

### 图形化分支管理
- 查看本地/远程分支：当前、已合并/未合并、领先/落后、上游。
- 创建并切换分支、切换分支（有未提交变更时自动阻止）、合并到当前（`--no-ff`）。
- 合并冲突：列出冲突文件，可一键「放弃合并」恢复原状。
- 删除分支：未合并分支需确认强制删除；当前分支不可删除。
- 拉取（仅快进）、推送当前分支、快照提交。

### 我的 GitHub 仓库
- 列出账号仓库（公开/私有、默认分支、更新时间），一键打开网页或克隆到本地扫描根目录。

## 数据位置

- 配置：`data\config.json`（roots / default_private）
- 凭据：`data\secret.bin`（DPAPI 加密）；日志：`data\logs\console.log`；运行标记：`data\server.pid`

## 安全边界

- 服务只监听 `127.0.0.1` 并校验 Host/Origin；写操作需要 HttpOnly 会话 Cookie。
- Token 仅用于你主动发起的 GitHub 操作（建仓/列表/克隆），不参与任何其他请求。
- 所有 git 操作只作用于「本地仓库」列表中你选择的目标目录。
