# 水墨国潮主题规范（摘自 02-shuimo-guochao.html，供 mytools 全家桶统一套用）

## 色板（浅色 · 宣纸）

| 用途 | 值 | 说明 |
|---|---|---|
| 页面底 paper | `#f4ecdc` | 宣纸米色 |
| 卡片/下沉区 paper-2 | `#ede3cf` | 稍深纸色 |
| 正文墨 ink | `#2b2419` | 深墨 |
| 次要文字 ink-soft | `#5d5343` | 淡墨 |
| 主强调 cinnabar | `#a33b2c` | 朱砂红：链接、主按钮、关键数字、印章、左边线 |
| 次强调 gold | `#b78b3d` | 金色：装饰线、菱形◆、小点缀 |
| 晕染 wash | `rgba(43,36,25,0.05)` | 水墨晕染装饰 |
| 边框 | `1px solid rgba(43,36,25,0.18)` | 卡片/单元格 |
| 分隔虚线 | `1px dashed rgba(43,36,25,0.14)` | 列表行分隔 |
| 阴影 | `0 10px 30px rgba(43,36,25,0.08)` | 卡片浮起 |

语义色映射：success → 苔绿 `#5f7351`；warning → gold `#b78b3d`；danger/error → cinnabar `#a33b2c`；info → 黛灰蓝 `#4a5d68`；accent/选中态/聚焦环 → cinnabar。

## 色板（深色 · 墨夜）——供 data-theme="dark" 使用

- 底 `#1c1813`，卡片 `#26211a`，下沉区 `#201b15`
- 正文 `#e6dac0`，次要 `#a89a80`
- cinnabar 提亮 `#c8543f`，gold 提亮 `#c9a35c`
- 边框 `rgba(230,218,192,0.14)`，虚线 `rgba(230,218,192,0.12)`
- 阴影 `0 10px 30px rgba(0,0,0,0.4)`

## 字体（必须本地回退，工具离线运行，禁止引入 Google Fonts 等外链）

- 正文：`'Noto Serif SC','Source Han Serif SC','STSong','SimSun',serif`
- 标题/品牌/大数字：`'ZCOOL XiaoWei','Ma Shan Zheng','STKaiti','KaiTi','STSong',serif`，配宽字距 `letter-spacing: 4px~10px`
- 等宽（端口/代码/数字表格）可保留原 mono 字体

## 风格元素（适度点缀，不要堆满界面）

1. 宣纸噪点背景：
   ```css
   background-image:
     radial-gradient(rgba(120,100,60,0.06) 1px, transparent 1.5px),
     radial-gradient(rgba(120,100,60,0.05) 1px, transparent 1.5px);
   background-size: 20px 20px, 20px 20px;
   background-position: 0 0, 10px 10px;
   ```
2. 页面/卡片顶部水墨晕染：`radial-gradient(600px 200px at 20% -40px, rgba(43,36,25,0.05), transparent 70%)`
3. 朱砂印章方块：`border:3px solid #a33b2c; color:#a33b2c; box-shadow:inset 0 0 0 1px #a33b2c;` 内放 2~4 个汉字（可用工具名，如「端口」「仓库」），适合页头/角落
4. 金色渐变分隔线：`background:linear-gradient(90deg,transparent,#b78b3d,transparent); height:1px;` 中央可放 ◆
5. 标题下短朱砂线：`width:70px; height:3px; background:#a33b2c;`
6. 圆角收敛：卡片 4~6px 小圆角（中式硬朗），不要大圆角
7. 列表行分隔用虚线；hover 上浮 `translateY(-2px)` + 纸色阴影

## 硬性约束

- 只改外观（颜色、字体、边框、阴影、背景、间距微调），**不改任何 JS 逻辑、接口、DOM id/class 命名**
- 保留明暗切换机制：light=宣纸，dark=墨夜
- 改完自查 CSS 括号配对、HTML 标签闭合
