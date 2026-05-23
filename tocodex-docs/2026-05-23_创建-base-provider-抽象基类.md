# 创建 BaseProvider 抽象基类

## 简要说明
新增一个可供各数据源 provider 继承的异步抽象基类 `BaseProvider`，使用 `abc.ABC` 定义统一接口，并补充启用判断、配置判断、演示数据构造与安全浮点转换等通用辅助方法。

本次实现同时根据当前 [`app/models.py`](app/models.py) 的现有模型结构，对演示返回值做了适配：
- `get_upcoming_matches()` 返回 `MatchRef` 列表
- `build_demo_match()` 返回 `MatchRef`
- `build_demo_snapshot()` 返回 `MatchSnapshot`

## 变更文件
- `app/services/providers/base.py`
- `tocodex-docs/2026-05-23_创建-base-provider-抽象基类.md`
