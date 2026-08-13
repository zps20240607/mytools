# CacheWatch 1.0 · 磁盘瘦身（Windows 版）

扫描并可视化 **node_modules / Python 缓存 / 各类工具缓存 / 模型目录 / .git 历史** 的磁盘占用，支持 **预览（dry-run）** 与 **安全一键清理**。

- **零依赖**：Python 3 标准库；前端无构建、无 CDN。
- **只绑定 127.0.0.1**，写操作校验 HttpOnly 会话 Cookie，勿对外暴露。
- **删除前严格校验**：目录名与类型一致、非 junction/符号链接、位于扫描范围或固定缓存白名单内。

## 快速开始

1. 双击 `启动控制台.bat`：后台静默启动（pythonw），自动打开浏览器，端口 9630–9639。
2. 双击 `停止控制台.bat` 停止服务。
3. 命令行方式：
   - `python cache_watch.py scan [--json]`：终端输出占用报告
   - `python cache_watch.py clean <id> [--dry-run]`：清理指定项目

## 清理策略

- **默认可清理（重新生成成本低）**：node_modules、`__pycache__`/`.pytest_cache` 等 Python 缓存、pip/uv/npm/pnpm/bun/Yarn/Go/Gradle/HuggingFace/NuGet/Electron 缓存。
- **默认不自动清理（需谨慎）**：Cargo registry、Ollama/LM Studio 模型、用户临时目录——模型重新下载成本高，仅在明确需要时手动处理。
- **.git 历史**：只展示大小，提供 `git gc` 安全压缩（≥50 MB 才可点）。
- 页面右上角「预览模式」默认开启：清理按钮只报告可释放空间，不真正删除；确认无误后取消勾选再执行。

## 数据位置

- 配置：`data\config.json`（roots）；日志：`data\logs\console.log`；运行标记：`data\server.pid`

## 安全边界

- 服务只监听 `127.0.0.1` 并校验 Host/Origin；写操作需要 HttpOnly 会话 Cookie。
- 所有删除动作都会经过 `_assert_safe_target` 校验，受保护目录（用户主目录、系统盘根、Windows 目录）一律拒绝。
