# Football Intelligence API

一个基于 FastAPI 的足球情报、赔率聚合与规则预测服务。

当前版本提供：
- 比赛基础信息聚合骨架
- 多赔率来源标准化骨架
- 规则型赛前分析与预测
- Docker / Compose / 一键部署脚本
- 在未配置真实 API Key 或上游失败时自动回退到 demo 数据

## 当前实现概览

项目入口：[`app.main:app`](app/main.py:48)

已实现的核心模块：
- 配置模块：[`app/config.py`](app/config.py:1)
- 共享模型：[`app/models.py`](app/models.py:1)
- 健康检查路由：[`app/routes/health.py`](app/routes/health.py:1)
- 比赛路由：[`app/routes/matches.py`](app/routes/matches.py:1)
- 预测路由：[`app/routes/predict.py`](app/routes/predict.py:1)
- 聚合层：[`app/services/aggregator.py`](app/services/aggregator.py:1)
- 预测器：[`app/services/predictor.py`](app/services/predictor.py:35)
- Provider 抽象基类：[`app/services/providers/base.py`](app/services/providers/base.py:1)
- football-data provider：[`app/services/providers/football_data.py`](app/services/providers/football_data.py:1)
- The Odds API provider：[`app/services/providers/the_odds_api.py`](app/services/providers/the_odds_api.py:1)
- Goalserve provider：[`app/services/providers/goalserve.py`](app/services/providers/goalserve.py:1)

## 功能特性

- 统一的比赛快照结构 [`MatchSnapshot`](app/models.py:58)
- 统一的赔率结构 [`OddsSnapshot`](app/models.py:37)
- 统一的分析输出结构 [`AnalysisResponse`](app/models.py:80)
- 使用多 provider 聚合赔率并归一化到 [`OddsPrice`](app/models.py:29)
- 提供基于近期战绩、进失球、伤停、赔率隐含概率的规则预测
- 支持直接传入手工参数进行预测，不依赖远程 match id
- 所有 provider 在 demo 模式下可稳定返回演示数据

## 环境要求

- Python 3.11+
- 可选：Docker / Docker Compose

## 本地运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 准备环境变量

复制示例配置：

```bash
cp .env.example .env
```

Windows CMD：

```bat
copy .env.example .env
```

### 3. 启动开发服务

```bash
uvicorn app.main:app --reload
```

默认地址：
- API: `http://127.0.0.1:8000`
- Swagger: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`

## Docker 部署

### 方式 1：直接使用 Docker Compose

```bash
docker compose up -d --build
```

相关文件：
- [`Dockerfile`](Dockerfile:1)
- [`docker-compose.yml`](docker-compose.yml:1)

### 方式 2：一键脚本

Windows：

```bat
deploy.bat
```

Linux / macOS：

```sh
sh deploy.sh
```

相关脚本：
- [`deploy.bat`](deploy.bat:1)
- [`deploy.sh`](deploy.sh:1)

## 环境变量

示例文件见 [`.env.example`](.env.example:1)。

主要变量如下：

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `APP_NAME` | 服务名称 | `football-intel-service` |
| `APP_ENV` | 运行环境 | `development` |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `DEMO_MODE` | 是否启用 demo 回退 | `true` |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时秒数 | `10` |
| `CACHE_TTL_SECONDS` | 缓存 TTL | `300` |
| `FOOTBALL_DATA_API_KEY` | football-data.org API Key | 空 |
| `FOOTBALL_DATA_BASE_URL` | football-data API 根地址 | `https://api.football-data.org/v4` |
| `THE_ODDS_API_KEY` | The Odds API Key | 空 |
| `THE_ODDS_BASE_URL` | The Odds API 根地址 | `https://api.the-odds-api.com/v4` |
| `GOALSERVE_API_KEY` | Goalserve API Key | 空 |
| `GOALSERVE_BASE_URL` | Goalserve API 根地址 | `https://www.goalserve.com/getfeed` |

## 已实现接口

### 健康与信息
- `GET /`
- `GET /health`
- `GET /sources`

### 比赛与快照
- `GET /matches/upcoming?date=YYYY-MM-DD`
- `GET /match/{match_id}/snapshot`
- `GET /odds/{match_id}`

### 预测与分析
- `GET /match/{match_id}/analysis`
- `POST /predict`

## 接口说明

### `GET /health`
返回服务基础状态、环境和已启用数据源。

### `GET /sources`
返回当前 provider 启用与配置状态。

### `GET /matches/upcoming`
返回即将开赛的比赛列表。未配置真实 provider 时会返回 demo 比赛。

### `GET /match/{match_id}/snapshot`
返回单场比赛的聚合快照，包括：
- 比赛基础信息
- 主客队近期状态
- 伤停信息
- 赛前备注
- 标准化赔率
- provider 证据链

### `GET /match/{match_id}/analysis`
基于聚合快照执行规则预测，输出：
- 胜平负倾向
- 推荐比分
- 大小球建议
- 亚指倾向
- 置信度评分
- 利好/风险因素

### `POST /predict`
支持两种模式：
1. 传入 `match_id`，由聚合层抓取比赛并分析
2. 不传 `match_id`，直接根据请求体构造手工快照并分析

示例请求：

```json
{
  "league": "Premier League",
  "kickoff": "2026-05-23T19:30:00Z",
  "home_team": "Team A",
  "away_team": "Team B",
  "home_recent_form": ["W", "W", "D", "L", "W"],
  "away_recent_form": ["L", "D", "W", "L", "D"],
  "over_under_line": 2.5,
  "handicap_line": -0.5
}
```

## Provider 说明

当前接入的是“可运行骨架 + demo fallback”模式：
- [`FootballDataProvider`](app/services/providers/football_data.py:11)
- [`TheOddsApiProvider`](app/services/providers/the_odds_api.py:19)
- [`GoalserveProvider`](app/services/providers/goalserve.py:19)

这些 provider 已具备：
- 统一初始化方式
- 与现有模型兼容的标准化输出
- API Key 缺失时自动回退
- 上游失败时不中断整体接口

但当前版本仍属于 MVP：
- 实际字段映射尚未覆盖全部真实上游响应
- 某些 provider 仍以 demo 数据为主
- 更完整的赛事统计、阵容、天气、伤停细节还可继续扩展

## 开发说明

如果只验证项目是否可导入，可执行：

```bash
python -c "from app.main import app; print(app.title)"
```

当前已验证 [`app.main`](app/main.py:1) 可成功导入，且核心路由已注册。

## 后续可扩展方向

- 增加更多真实数据源字段映射
- 接入 Redis 缓存
- 增加 xG / Elo / Poisson 等模型
- 完善 Docker 健康检查与日志配置
- 增加测试与 CI
- 增加鉴权、限流与持久化

## 说明

当前实现优先保证：
1. 项目结构清晰
2. 接口统一
3. provider 失败不拖垮整体服务
4. 未配置 Key 时也能本地演示与部署

因此，如果没有配置真实 API Key，接口返回 demo 数据是预期行为。
