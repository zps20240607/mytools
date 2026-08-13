# EnvWatch 1.0 · 环境体检台（Windows 版）

汇总本机开发环境状态：**工具版本 / PATH 冲突 / 失效目录 / Store 垫片 / 系统资源**，生成 HTML 体检报告并在控制台输出摘要。

- **零依赖**：Python 3 标准库，不安装任何包。
- **纯本地**：所有检测只读本机信息，不上传任何数据。

## 快速开始

1. 双击 `生成体检报告.bat`：扫描环境 → 输出控制台摘要 → 生成并自动打开 `data\report.html`。
2. 命令行方式：
   - `python env_watch.py`（扫描 + 摘要 + 打开报告）
   - `python env_watch.py --no-browser`（只生成不打开）
   - `python env_watch.py --json`（输出 JSON，便于 AI 读取）

## 检测内容

- **工具版本**：Python / Node / uv / pnpm / Git / gh / Docker / WSL / ffmpeg / pandoc / rg / fzf / Ollama / VS Code 等 45+ 工具。
- **Python 体检**：多份 python.exe、Store 垫片、python3 别名缺失。
- **PATH 体检**：失效目录、重复条目、WindowsApps 垫片目录、总长度超限。
- **系统体检**：OS / CPU / 内存、系统盘剩余空间、临时目录可写性。
- **建议项**：uv、WSL、Docker 等对 vibe coding 帮助大的缺失工具给出安装提示。

## 文件位置

- 报告：`data\report.html`（每次生成覆盖，git 已忽略）
