# 创建 zgzcw-provider

创建了足彩网 (zgzcw.com) 数据抓取器。

## 变更文件

- [`app/services/providers/zgzcw.py`](app/services/providers/zgzcw.py) — 新建，ZgzcwProvider 类

## 实现要点

- 继承 `BaseProvider`，实现 `get_upcoming_matches`、`get_match_snapshot`、`get_match_odds`
- 从 `plzx.zgzcw.com/bjzs/` 百家指数页抓取 HTML 表格
- 解析 30 场比赛的编号、联赛、时间、主客队、欧赔初盘和即时盘
- 支持箭头趋势解析（↑↓ → up/down）
- match_id 从链接中提取，格式为 `zgzcw-{7位数字}`
- 编码自动检测（meta charset → Content-Type → 默认 gbk）
- 数据源不可用时返回 demo 数据