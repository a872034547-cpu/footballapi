# 创建 njstats-provider

创建了 `app/services/providers/njstats.py`，实现 `NjstatsProvider` 类。

## 变更文件

- [`app/services/providers/njstats.py`](app/services/providers/njstats.py) — 新建

## 实现要点

- 继承 `BaseProvider`，遵循 `wubai.py` 的代码风格
- **双模式运行**: 检测 `NJSTATS_API_KEY` / `NJSTATS_TOKEN` 环境变量，有则尝试真实 API，无则 demo 模式
- **真实 API 模式**: 依次尝试 12 个已知端点，支持通用 JSON 解析（多种字段名映射）
- **Demo 模式**: 返回 5 场五大联赛模拟比赛数据，含战绩、伤停、天气、情报笔记、赔率
- 通过 `logging` 清晰输出当前运行模式
- `is_configured()` 检查 API key 是否存在；`is_enabled()` 检查 `njstats_enabled` 配置项