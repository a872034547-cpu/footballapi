# zgzcw + njstats 数据源集成完成

## 概述
成功逆向并集成两个新的中国足球数据网站作为数据提供方，服务现支持 6 个数据源。

## 新增文件
- [`app/services/providers/zgzcw.py`](app/services/providers/zgzcw.py) — 足彩网百家指数抓取器 (531行)
- [`app/services/providers/njstats.py`](app/services/providers/njstats.py) — njstats.cn 数据提供方 (862行)

## 修改文件
- [`app/config.py`](app/config.py) — 新增 zgzcw_enabled, zgzcw_bjzs_url, zgzcw_live_url, njstats_enabled, njstats_api_key, njstats_base_url 配置项
- [`app/services/aggregator.py`](app/services/aggregator.py) — 注册 ZgzcwProvider 和 NjstatsProvider

## 关键发现
- **zgzcw.com**: `plzx.zgzcw.com/bjzs/` 无 WAF，直接返回 30 场比赛的 HTML 表格，含初盘+即时盘欧赔及箭头趋势（↑↓→）
- **njstats.cn**: Vue/uni-app SPA，所有 API 需认证（401），实现 demo 模式回退

## Bug 修复
- `_strip_arrow()` 未处理 `→` (U+2192, stable) 箭头，导致 `float("1.81→")` 失败返回 0.0
- 修复：新增 `→` 精确匹配 + rstrip 回退 + 趋势标签映射

## 验证结果
- 6 个数据源全部启用
- 106 场比赛（zgzcw: 30 真实, njstats: 5 demo, wubai: 67, 其他: 4）
- 快照聚合 34 条赔率记录
- 分析端点正常：confidence=92.0, WDL 预测 home_win=54.94%