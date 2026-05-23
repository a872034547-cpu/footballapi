# 2026-05-23 sync.php 新增 --rolling 和 --live 参数

## 修改文件
- [`php-predict/sync.php`](php-predict/sync.php)

## 变更摘要

### 新增参数
- `--rolling`：同步滚球预测，从 SQLite 读取进行中且 minute >= 10 的比赛，调用 FastAPI `/match/{id}/rolling` 获取滚球预测并写入 `rolling_predictions` 表
- `--live`：刷新所有进行中比赛的实时状态，调用 FastAPI `/matches/live` 获取实时比分，更新 `matches` 表的 `home_score`、`away_score`、`minute`、`status` 字段

### 新增函数
- `sync_rolling_predictions(): void` — 滚球预测同步逻辑
- `sync_live_status(): void` — 实时状态同步逻辑

### 新增数据库表
- `rolling_predictions`：存储滚球预测结果（match_id, minute, score, prediction_type, direction, probability, rationale, confidence_level）

### 兼容性处理
- 自动 `ALTER TABLE matches ADD COLUMN minute` 兼容旧表结构