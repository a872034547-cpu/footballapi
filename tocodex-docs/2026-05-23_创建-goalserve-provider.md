# 创建 Goalserve Provider

## 摘要
已创建 [`GoalserveProvider`](app/services/providers/goalserve.py:1)，用于提供稳定的 Goalserve 赔率接口占位实现。该实现默认优先保证可用性：在无 API Key、启用 demo 模式或远端请求失败时，统一回退到 demo 赔率数据，不向外抛出异常。

## 变更文件
- [`app/services/providers/goalserve.py`](app/services/providers/goalserve.py:1)

## 实现要点
- 提供 [`name = "goalserve"`](app/services/providers/goalserve.py:37)
- 实现异步 [`get_match_odds`](app/services/providers/goalserve.py:61)，主返回值为 `OddsSnapshot` 风格数据
- [`get_upcoming_matches`](app/services/providers/goalserve.py:55) 与 [`get_match_snapshot`](app/services/providers/goalserve.py:58) 返回 demo 数据
- demo 赔率包含欧指、亚指、大小球，且 bookmaker 固定为 `goalserve-demo`
- 保留 [`httpx.AsyncClient`](app/services/providers/goalserve.py:88) 远端调用占位，便于后续接入真实 key
- 所有远端失败场景均回退 demo，确保 provider 对外稳定
