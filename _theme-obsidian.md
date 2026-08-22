# mytools 控制台主题规范 —— 「曜石 / Obsidian」现代科技风

适用：PortWatch / RepoWatch / CacheWatch / CostWatch / GitMate / TokenWatch / EnvWatch
（TodoWatch 每日清单保持水墨主题，不适用本规范）

设计基调：数据控制台。深色如 Linear/Vercel 仪表盘：墨黑底、细发丝边框、8~10px 圆角、
克制阴影、数字用等宽字体；浅色模式为清爽灰白底。每个工具一个专属强调色（accent），
其余中性色完全一致，保证全家桶观感统一。

## 各工具强调色

| 工具 | accent（深色模式） | accent（浅色模式） | 含义 |
|---|---|---|---|
| PortWatch 端口监控 | `#4fa3ff` | `#2470d6` | 信号青蓝 |
| RepoWatch 仓库总控台 | `#8b7cf6` | `#6a5ae0` | 分支紫 |
| CacheWatch 磁盘瘦身 | `#34c08c` | `#1e9e70` | 清理绿 |
| CostWatch 成本核算 | `#f0a03c` | `#c77f1d` | 账单琥珀 |
| GitMate GitHub助手 | `#3fb950` | `#2da44e` | GitHub 绿 |
| TokenWatch Token统计 | `#e56399` | `#c9407e` | 计量玫红 |
| EnvWatch 环境体检台 | `#39c5cf` | `#1899a3` | 体检青 |

## 深色模式（data-theme="dark" 或默认深色页）

- 页面底 `--bg`: `#0f1216`
- 卡片 `--card`: `#171b21`；下沉/次卡 `--card-2`: `#1d232b`
- 正文 `--ink`: `#e6e9ee`；次要 `--ink-2`: `#9aa4b0`；弱 `--ink-3`: `#6b7580`
- 边框 `--line`: `rgba(255,255,255,0.08)`；强边框 `--line-2`: `rgba(255,255,255,0.16)`
- 阴影：`0 1px 2px rgba(0,0,0,0.4)`，浮起 `0 12px 32px rgba(0,0,0,0.5)`
- 语义色：success `#34c08c`、warning `#f0a03c`、danger `#e5534b`、info `#4fa3ff`
- 聚焦环：`0 0 0 2.5px <accent 33% 透明度>`
- 背景可加极淡的顶部光晕：`radial-gradient(800px 300px at 50% -120px, <accent 8% 透明度>, transparent 70%)`

## 浅色模式

- 页面底 `#f5f6f8`；卡片 `#ffffff`；下沉 `#eef0f3`
- 正文 `#1a1f26`；次要 `#5b6672`；弱 `#8a94a0`
- 边框 `rgba(15,23,42,0.09)`；强边框 `rgba(15,23,42,0.18)`
- 阴影：`0 1px 2px rgba(15,23,42,0.06)`，浮起 `0 12px 32px rgba(15,23,42,0.12)`
- 语义色同上（浅色下 danger 可用 `#d1453d`）

## 字体与形状

- 正文字体保持系统栈：`-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif`
- 数字/端口/代码用等宽：ui-monospace / Consolas / 工具自带 mono 字体
- 圆角：卡片/按钮 8~10px，小标签 5~6px；不要大圆角
- 按钮：主按钮 = accent 底白字；次按钮透明底 + 发丝边框
- 禁止外链字体/图标（离线运行）

## 硬约束

- 只改外观（颜色、字体、边框、阴影、圆角、背景装饰），不改 JS 逻辑、接口、DOM 的 id/class
- 保留明暗切换（theme.js / data-theme 机制）：浅色深色都要成套
- 改完自查 CSS 括号配对、HTML 标签闭合；python 文件改动后用 ast.parse 验证
