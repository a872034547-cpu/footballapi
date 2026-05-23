"""njstats.cn 足球数据提供方 —— 认证 API + Demo 回退。

njstats.cn 是一个高级足球数据网站 (Vue/uni-app SPA, Spring Boot 后端)，
所有 API 端点都需要完整认证 (401 "Full authentication is required to access this resource")。

已知 API 端点 (全部 401):
  /api/index, /api/home, /api/matches, /api/match/list
  /api/football/matches, /api/data/index
  /api/v1/matches, /api/v2/matches
  /api/public/index, /api/public/matches
  /api/match/query, /api/data/query
  /api/user/info, /api/login, /api/register

已知路由页面 (14个):
  /pages/index/index (首页), /pages/detail/index (详情),
  /pages/match/index (比赛), /pages/leagues/index (联赛),
  /pages/teams/index (球队), /pages/players/index (球员),
  /pages/intelligence/index (情报), /pages/lottery/index (彩票),
  /pages/value/index (价值评估), /pages/xuanpan/index (选盘),
  /pages/recalling/index (复盘), /pages/dev/shotMap (射门图),
  /pages/tutorial/index (教程)

实现策略:
  1. 如果配置了 NJSTATS_API_KEY 或 NJSTATS_TOKEN 环境变量，尝试调用真实 API
  2. 否则返回 demo 数据
  3. 通过日志清晰说明当前运行模式
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.models import (
    InjuryNote,
    MatchRef,
    MatchSnapshot,
    OddsPrice,
    OddsSnapshot,
    TeamForm,
    TeamRef,
)
from app.services.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────
NJSTATS_BASE_URL = "https://www.njstats.cn"

# 已知的 API 端点列表 (全部需要认证)
KNOWN_ENDPOINTS = [
    "/api/index",
    "/api/home",
    "/api/matches",
    "/api/match/list",
    "/api/football/matches",
    "/api/data/index",
    "/api/v1/matches",
    "/api/v2/matches",
    "/api/public/index",
    "/api/public/matches",
    "/api/match/query",
    "/api/data/query",
]

HEADERS_TEMPLATE: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": NJSTATS_BASE_URL,
    "Referer": f"{NJSTATS_BASE_URL}/",
}


# ── 工具函数 ──────────────────────────────────────────
def _get_api_key() -> str | None:
    """从环境变量读取 njstats 认证凭据。

    优先级: NJSTATS_API_KEY > NJSTATS_TOKEN
    """
    return os.environ.get("NJSTATS_API_KEY") or os.environ.get("NJSTATS_TOKEN") or None


def _build_auth_headers(api_key: str) -> dict[str, str]:
    """构建带认证的请求头。

    尝试多种常见的认证头格式:
      - Authorization: Bearer <token>
      - X-API-Key: <token>
      - token: <token>
    """
    headers = dict(HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {api_key}"
    headers["X-API-Key"] = api_key
    headers["token"] = api_key
    return headers


# ── Provider ──────────────────────────────────────────
class NjstatsProvider(BaseProvider):
    """njstats.cn 足球数据提供方。

    由于 njstats.cn 所有 API 端点都需要完整认证且无法公开注册，
    此 Provider 默认运行在 demo 模式。如果通过环境变量配置了有效的
    API key，则会尝试调用真实 API。

    环境变量:
      NJSTATS_API_KEY  - API 认证密钥 (优先)
      NJSTATS_TOKEN    - 备选认证令牌
    """

    name = "njstats"

    # ── 内部状态 ────────────────────────────────────
    _api_key: str | None = None
    _demo_mode: bool = True

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._api_key = _get_api_key()
        self._demo_mode = self._api_key is None

        if self._demo_mode:
            logger.info(
                "njstats: 未检测到 NJSTATS_API_KEY / NJSTATS_TOKEN 环境变量，"
                "运行在 demo 模式。设置环境变量后可切换为真实 API 模式。"
            )
        else:
            logger.info(
                "njstats: 检测到 API key (长度=%d)，将尝试调用真实 API。",
                len(self._api_key),
            )

    # ── 配置检查 ────────────────────────────────────
    def is_configured(self) -> bool:
        """检查是否有可用的 API 认证凭据。"""
        return self._api_key is not None

    def is_enabled(self) -> bool:
        """njstats 始终可启用 (demo 模式兜底)。"""
        # 检查 settings 中的 njstats_enabled 配置项
        enabled = getattr(self.settings, "njstats_enabled", True)
        return bool(enabled)

    @property
    def demo_mode(self) -> bool:
        """当前是否运行在 demo 模式。"""
        return self._demo_mode

    # ── 比赛列表 ────────────────────────────────────
    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        """获取即将开始的比赛列表。

        真实 API 模式: 尝试调用 /api/matches 等端点
        Demo 模式: 返回模拟数据
        """
        if not self._demo_mode and self._api_key:
            try:
                return await self._fetch_upcoming_matches(date)
            except Exception as exc:
                logger.warning("njstats: 真实 API 调用失败: %s", exc)

        return []

    async def _fetch_upcoming_matches(self, date: str | None) -> list[MatchRef]:
        """尝试从 njstats.cn 真实 API 获取比赛列表。

        依次尝试已知的端点，直到某个端点返回 200。
        """
        headers = _build_auth_headers(self._api_key)  # type: ignore[arg-type]
        timeout = getattr(self.settings, "request_timeout_seconds", 15)

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            for endpoint in KNOWN_ENDPOINTS:
                url = f"{NJSTATS_BASE_URL}{endpoint}"
                try:
                    resp = await client.get(url, headers=headers, follow_redirects=True)
                    if resp.status_code == 200:
                        logger.info("njstats: 端点 %s 返回 200，尝试解析数据", endpoint)
                        return self._parse_api_matches(resp.json(), date)
                    elif resp.status_code == 401:
                        logger.debug("njstats: 端点 %s 返回 401 (认证失败)", endpoint)
                    else:
                        logger.debug("njstats: 端点 %s 返回 %d", endpoint, resp.status_code)
                except httpx.TimeoutException:
                    logger.debug("njstats: 端点 %s 超时", endpoint)
                except Exception as exc:
                    logger.debug("njstats: 端点 %s 异常: %s", endpoint, exc)

        raise RuntimeError("所有已知 njstats.cn 端点均无法访问或认证失败")

    def _parse_api_matches(self, data: Any, filter_date: str | None) -> list[MatchRef]:
        """解析 njstats.cn API 返回的比赛数据。

        njstats.cn 的响应结构未知，这里做最大努力的通用解析:
          - 如果 data 是 list，遍历每个元素
          - 如果 data 是 dict，尝试 data['data'], data['matches'], data['list'] 等常见键名
        """
        items: list[dict] = []

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("data", "matches", "list", "result", "records", "items"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
            if not items:
                # 尝试把整个 dict 当作单条记录
                items = [data]

        matches: list[MatchRef] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                match = self._parse_single_match(item, filter_date)
                if match:
                    matches.append(match)
            except Exception:
                continue

        return matches

    def _parse_single_match(self, item: dict, filter_date: str | None) -> MatchRef | None:
        """从单条 API 记录解析 MatchRef。

        尝试多种可能的字段名映射。
        """
        # 比赛 ID
        match_id = str(
            item.get("id")
            or item.get("matchId")
            or item.get("match_id")
            or item.get("fixtureId")
            or item.get("fixture_id")
            or ""
        )
        if not match_id:
            return None

        # 开赛时间
        kickoff = str(
            item.get("kickoff")
            or item.get("kickoffTime")
            or item.get("kickoff_time")
            or item.get("matchTime")
            or item.get("match_time")
            or item.get("startTime")
            or item.get("start_time")
            or ""
        )

        # 日期过滤
        if filter_date and kickoff:
            if not kickoff.startswith(filter_date):
                return None

        # 联赛
        league = str(
            item.get("league")
            or item.get("leagueName")
            or item.get("league_name")
            or item.get("competition")
            or item.get("competitionName")
            or item.get("competition_name")
            or ""
        )

        # 主队
        home_raw = item.get("homeTeam") or item.get("home_team") or item.get("home") or {}
        if isinstance(home_raw, dict):
            home_id = str(home_raw.get("id") or home_raw.get("teamId") or home_raw.get("team_id") or "")
            home_name = str(home_raw.get("name") or home_raw.get("teamName") or home_raw.get("team_name") or "")
            home_short = str(home_raw.get("shortName") or home_raw.get("short_name") or home_raw.get("abbr") or "")
        elif isinstance(home_raw, str):
            home_id = ""
            home_name = home_raw
            home_short = ""
        else:
            home_id = ""
            home_name = ""
            home_short = ""

        # 客队
        away_raw = item.get("awayTeam") or item.get("away_team") or item.get("away") or {}
        if isinstance(away_raw, dict):
            away_id = str(away_raw.get("id") or away_raw.get("teamId") or away_raw.get("team_id") or "")
            away_name = str(away_raw.get("name") or away_raw.get("teamName") or away_raw.get("team_name") or "")
            away_short = str(away_raw.get("shortName") or away_raw.get("short_name") or away_raw.get("abbr") or "")
        elif isinstance(away_raw, str):
            away_id = ""
            away_name = away_raw
            away_short = ""
        else:
            away_id = ""
            away_name = ""
            away_short = ""

        # 状态
        status = str(item.get("status") or item.get("matchStatus") or item.get("match_status") or "SCHEDULED")

        # 场地
        venue = str(item.get("venue") or item.get("stadium") or item.get("location") or "")

        return MatchRef(
            id=match_id,
            league=league,
            kickoff=kickoff,
            home_team=TeamRef(id=home_id, name=home_name, short_name=home_short or None),
            away_team=TeamRef(id=away_id, name=away_name, short_name=away_short or None),
            status=status,
            venue=venue or None,
        )

    # ── 比赛快照 ────────────────────────────────────
    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        """获取单场比赛的聚合快照。

        真实 API 模式: 尝试调用 /api/match/query 等端点
        Demo 模式: 返回模拟数据
        """
        if not self._demo_mode and self._api_key:
            try:
                return await self._fetch_match_snapshot(match_id)
            except Exception as exc:
                logger.warning(
                    "njstats: 获取比赛快照失败 (match_id=%s)，回退到 demo 数据: %s",
                    match_id, exc,
                )

        return self._build_demo_snapshot(match_id)

    async def _fetch_match_snapshot(self, match_id: str) -> MatchSnapshot:
        """尝试从 njstats.cn 真实 API 获取比赛快照。"""
        headers = _build_auth_headers(self._api_key)  # type: ignore[arg-type]
        timeout = getattr(self.settings, "request_timeout_seconds", 15)

        # 尝试多个可能的详情端点
        detail_endpoints = [
            f"/api/match/query?matchId={match_id}",
            f"/api/data/query?matchId={match_id}",
            f"/api/matches/{match_id}",
            f"/api/v1/matches/{match_id}",
            f"/api/v2/matches/{match_id}",
            f"/api/football/matches/{match_id}",
        ]

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            for endpoint in detail_endpoints:
                url = f"{NJSTATS_BASE_URL}{endpoint}"
                try:
                    resp = await client.get(url, headers=headers, follow_redirects=True)
                    if resp.status_code == 200:
                        logger.info("njstats: 快照端点 %s 返回 200", endpoint)
                        return self._parse_api_snapshot(resp.json(), match_id)
                except Exception as exc:
                    logger.debug("njstats: 快照端点 %s 异常: %s", endpoint, exc)

        raise RuntimeError(f"无法从 njstats.cn 获取比赛 {match_id} 的快照")

    def _parse_api_snapshot(self, data: Any, match_id: str) -> MatchSnapshot:
        """解析 njstats.cn API 返回的比赛快照数据。"""
        item: dict = {}
        if isinstance(data, dict):
            item = data.get("data", data)
        elif isinstance(data, list) and data:
            item = data[0] if isinstance(data[0], dict) else {}

        # 解析比赛基本信息
        match_ref = self._parse_single_match(item, None) or MatchRef(
            id=match_id,
            league="",
            kickoff="",
            home_team=TeamRef(id="", name=""),
            away_team=TeamRef(id="", name=""),
            status="SCHEDULED",
        )

        # 近期战绩
        home_form = self._parse_team_form(item, prefix="home")
        away_form = self._parse_team_form(item, prefix="away")

        # 伤停
        injuries = self._parse_injuries(item)

        # 天气
        weather = str(item.get("weather") or item.get("weatherInfo") or "")

        # 赛前笔记
        notes: list[str] = []
        for key in ("notes", "preMatchNotes", "pre_match_notes", "summary", "intelligence"):
            val = item.get(key)
            if isinstance(val, list):
                notes.extend(str(v) for v in val)
            elif isinstance(val, str) and val:
                notes.append(val)

        # 赔率
        odds = self._parse_odds_from_snapshot(item, match_id)

        return MatchSnapshot(
            match=match_ref,
            home_form=home_form,
            away_form=away_form,
            injuries=injuries,
            weather=weather or None,
            pre_match_notes=notes,
            odds=odds,
            provider_evidence=[f"njstats:api:{match_id}"],
        )

    def _parse_team_form(self, item: dict, prefix: str) -> TeamForm:
        """从 API 数据中解析球队近期战绩。"""
        form_key = f"{prefix}_form" if prefix else "form"
        recent_key = f"{prefix}_recent" if prefix else "recent"

        # 尝试获取 last_5
        last_5_raw = (
            item.get(f"{form_key}_last_5")
            or item.get(f"{recent_key}_last_5")
            or item.get(f"{prefix}Last5")
            or item.get(f"{prefix}Form")
            or item.get(form_key)
            or []
        )

        last_5: list[str] = []
        if isinstance(last_5_raw, list):
            last_5 = [str(r) for r in last_5_raw]
        elif isinstance(last_5_raw, str):
            # 可能是 "WDLWW" 这样的字符串
            last_5 = list(last_5_raw)

        # 统计数据
        ppm = self._safe_float(
            item.get(f"{prefix}_ppm")
            or item.get(f"{prefix}Ppm")
            or item.get(f"{prefix}_points_per_match")
            or 0.0
        )
        gf = self._safe_float(
            item.get(f"{prefix}_gf_avg")
            or item.get(f"{prefix}GoalsForAvg")
            or item.get(f"{prefix}_goals_for_avg")
            or 0.0
        )
        ga = self._safe_float(
            item.get(f"{prefix}_ga_avg")
            or item.get(f"{prefix}GoalsAgainstAvg")
            or item.get(f"{prefix}_goals_against_avg")
            or 0.0
        )

        return TeamForm(
            last_5=last_5,
            points_per_match=ppm,
            goals_for_avg=gf,
            goals_against_avg=ga,
        )

    def _parse_injuries(self, item: dict) -> list[InjuryNote]:
        """从 API 数据中解析伤停信息。"""
        injuries_raw = item.get("injuries") or item.get("injuryList") or item.get("injury_list") or []
        if not isinstance(injuries_raw, list):
            return []

        result: list[InjuryNote] = []
        for inj in injuries_raw:
            if isinstance(inj, dict):
                result.append(InjuryNote(
                    team=str(inj.get("team") or inj.get("teamName") or ""),
                    player=str(inj.get("player") or inj.get("playerName") or inj.get("name") or ""),
                    status=str(inj.get("status") or inj.get("injuryStatus") or ""),
                    impact=str(inj.get("impact") or inj.get("severity") or "medium"),
                ))
            elif isinstance(inj, str):
                result.append(InjuryNote(
                    team="",
                    player=inj,
                    status="unknown",
                    impact="medium",
                ))

        return result

    def _parse_odds_from_snapshot(self, item: dict, match_id: str) -> OddsSnapshot | None:
        """从快照数据中解析赔率信息。"""
        odds_raw = item.get("odds") or item.get("oddsData") or item.get("odds_data")
        if not odds_raw:
            return None

        prices: list[OddsPrice] = []

        if isinstance(odds_raw, dict):
            # 尝试解析 1x2
            for market_key, market_name in [("1x2", "1x2"), ("european", "1x2"), ("ouzhi", "1x2")]:
                market_data = odds_raw.get(market_key)
                if isinstance(market_data, dict):
                    for sel in ("home", "draw", "away"):
                        val = market_data.get(sel)
                        if val is not None:
                            prices.append(OddsPrice(
                                bookmaker="njstats",
                                market="1x2",
                                selection=sel,
                                line=None,
                                price=self._safe_float(val),
                            ))

            # 尝试解析亚盘
            for market_key in ("asian_handicap", "asianHandicap", "yazhi", "handicap"):
                market_data = odds_raw.get(market_key)
                if isinstance(market_data, dict):
                    line_val = self._safe_float(market_data.get("line", 0))
                    for sel in ("home", "away"):
                        val = market_data.get(sel)
                        if val is not None:
                            prices.append(OddsPrice(
                                bookmaker="njstats",
                                market="asian_handicap",
                                selection=sel,
                                line=line_val,
                                price=self._safe_float(val),
                            ))

            # 尝试解析大小球
            for market_key in ("over_under", "overUnder", "totals", "daxiaoqiu"):
                market_data = odds_raw.get(market_key)
                if isinstance(market_data, dict):
                    line_val = self._safe_float(market_data.get("line", 2.5))
                    for sel in ("over", "under"):
                        val = market_data.get(sel)
                        if val is not None:
                            prices.append(OddsPrice(
                                bookmaker="njstats",
                                market="over_under",
                                selection=sel,
                                line=line_val,
                                price=self._safe_float(val),
                            ))

        if not prices:
            return None

        return OddsSnapshot(
            match_id=match_id,
            prices=prices,
            last_update=datetime.now(timezone.utc).isoformat(),
            source="njstats",
        )

    # ── 赔率数据 ────────────────────────────────────
    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        """获取单场比赛的赔率快照。

        真实 API 模式: 尝试调用 API 获取赔率
        Demo 模式: 返回模拟赔率数据
        """
        if not self._demo_mode and self._api_key:
            try:
                # 先尝试获取完整快照，从中提取赔率
                snapshot = await self._fetch_match_snapshot(match_id)
                if snapshot.odds:
                    return snapshot.odds
            except Exception as exc:
                logger.warning(
                    "njstats: 获取赔率失败 (match_id=%s)，回退到 demo 数据: %s",
                    match_id, exc,
                )

        return self._build_demo_odds(match_id)

    # ── Demo 数据构建 ───────────────────────────────
    def _build_demo_matches(self, date: str | None = None) -> list[MatchRef]:
        """构建 demo 比赛列表。

        模拟 njstats.cn 可能返回的数据结构，包含多场比赛。
        """
        today = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        demo_matches = [
            MatchRef(
                id="njstats-demo-001",
                league="英超",
                kickoff=f"{today}T19:30:00Z",
                home_team=TeamRef(id="nj-h-001", name="曼城", short_name="MCI"),
                away_team=TeamRef(id="nj-a-001", name="利物浦", short_name="LIV"),
                status="SCHEDULED",
                venue="伊蒂哈德球场",
            ),
            MatchRef(
                id="njstats-demo-002",
                league="西甲",
                kickoff=f"{today}T20:00:00Z",
                home_team=TeamRef(id="nj-h-002", name="巴塞罗那", short_name="BAR"),
                away_team=TeamRef(id="nj-a-002", name="皇家马德里", short_name="RMA"),
                status="SCHEDULED",
                venue="诺坎普球场",
            ),
            MatchRef(
                id="njstats-demo-003",
                league="德甲",
                kickoff=f"{today}T17:30:00Z",
                home_team=TeamRef(id="nj-h-003", name="拜仁慕尼黑", short_name="BAY"),
                away_team=TeamRef(id="nj-a-003", name="多特蒙德", short_name="DOR"),
                status="SCHEDULED",
                venue="安联球场",
            ),
            MatchRef(
                id="njstats-demo-004",
                league="意甲",
                kickoff=f"{today}T18:00:00Z",
                home_team=TeamRef(id="nj-h-004", name="尤文图斯", short_name="JUV"),
                away_team=TeamRef(id="nj-a-004", name="国际米兰", short_name="INT"),
                status="SCHEDULED",
                venue="安联竞技场",
            ),
            MatchRef(
                id="njstats-demo-005",
                league="法甲",
                kickoff=f"{today}T20:45:00Z",
                home_team=TeamRef(id="nj-h-005", name="巴黎圣日耳曼", short_name="PSG"),
                away_team=TeamRef(id="nj-a-005", name="马赛", short_name="MAR"),
                status="SCHEDULED",
                venue="王子公园球场",
            ),
        ]

        logger.info("njstats: demo 模式 — 返回 %d 场模拟比赛", len(demo_matches))
        return demo_matches

    def _build_demo_snapshot(self, match_id: str) -> MatchSnapshot:
        """构建 demo 比赛快照。

        根据 match_id 返回不同的模拟数据，模拟 njstats.cn 的情报分析风格。
        """
        # 根据 match_id 选择不同的 demo 数据
        demo_index = 0
        for i, ch in enumerate(match_id):
            if ch.isdigit():
                demo_index = (int(ch) - 1) % 5
                break

        demo_configs = [
            {
                "league": "英超",
                "home": ("曼城", "MCI"),
                "away": ("利物浦", "LIV"),
                "venue": "伊蒂哈德球场",
                "home_form": ["W", "W", "D", "W", "W"],
                "away_form": ["W", "D", "W", "L", "W"],
                "home_ppm": 2.6,
                "away_ppm": 2.0,
                "home_gf": 2.4,
                "away_gf": 2.0,
                "home_ga": 0.6,
                "away_ga": 0.8,
                "weather": "晴, 18°C, 湿度 55%",
                "notes": [
                    "曼城近5场主场全胜，场均进球 2.8",
                    "利物浦主力前锋伤愈复出，状态待观察",
                    "历史交锋: 近10场曼城 4胜3平3负，略占优势",
                    "njstats 情报: 曼城控球率联赛第一 (62.3%)",
                ],
                "injuries": [
                    InjuryNote(team="曼城", player="德布劳内", status="doubtful", impact="high"),
                    InjuryNote(team="利物浦", player="范迪克", status="out", impact="high"),
                ],
            },
            {
                "league": "西甲",
                "home": ("巴塞罗那", "BAR"),
                "away": ("皇家马德里", "RMA"),
                "venue": "诺坎普球场",
                "home_form": ["W", "L", "W", "D", "W"],
                "away_form": ["W", "W", "W", "D", "W"],
                "home_ppm": 2.0,
                "away_ppm": 2.6,
                "home_gf": 2.0,
                "away_gf": 2.2,
                "home_ga": 1.0,
                "away_ga": 0.6,
                "weather": "多云, 22°C, 湿度 60%",
                "notes": [
                    "国家德比，双方战意极强",
                    "巴萨主场对皇马近5场 2胜2平1负",
                    "皇马近期客场4连胜，状态火热",
                    "njstats 情报: 巴萨控球率 58.7%，皇马反击效率联赛第一",
                ],
                "injuries": [
                    InjuryNote(team="巴塞罗那", player="佩德里", status="out", impact="medium"),
                    InjuryNote(team="皇家马德里", player="米利唐", status="doubtful", impact="medium"),
                ],
            },
            {
                "league": "德甲",
                "home": ("拜仁慕尼黑", "BAY"),
                "away": ("多特蒙德", "DOR"),
                "venue": "安联球场",
                "home_form": ["W", "W", "W", "L", "W"],
                "away_form": ["D", "W", "L", "W", "D"],
                "home_ppm": 2.4,
                "away_ppm": 1.6,
                "home_gf": 2.8,
                "away_gf": 1.6,
                "home_ga": 0.8,
                "away_ga": 1.4,
                "weather": "小雨, 12°C, 湿度 78%",
                "notes": [
                    "拜仁主场对多特近10场 8胜1平1负，碾压优势",
                    "多特蒙德客场防守不稳，近5客场丢球 9个",
                    "拜仁主力中锋状态火热，连续5场进球",
                    "njstats 情报: 拜仁射门转化率 18.2%，德甲第一",
                ],
                "injuries": [
                    InjuryNote(team="拜仁慕尼黑", player="诺伊尔", status="out", impact="high"),
                    InjuryNote(team="多特蒙德", player="阿莱", status="doubtful", impact="low"),
                ],
            },
            {
                "league": "意甲",
                "home": ("尤文图斯", "JUV"),
                "away": ("国际米兰", "INT"),
                "venue": "安联竞技场",
                "home_form": ["D", "W", "W", "D", "W"],
                "away_form": ["W", "W", "L", "W", "W"],
                "home_ppm": 2.2,
                "away_ppm": 2.4,
                "home_gf": 1.4,
                "away_gf": 2.0,
                "home_ga": 0.6,
                "away_ga": 0.8,
                "weather": "晴, 20°C, 湿度 45%",
                "notes": [
                    "意大利国家德比，双方防守稳固",
                    "尤文近5场仅丢3球，防守端表现出色",
                    "国米客场进攻火力强劲，场均 1.8 球",
                    "njstats 情报: 尤文定位球得分率意甲第一",
                ],
                "injuries": [
                    InjuryNote(team="尤文图斯", player="基耶萨", status="doubtful", impact="medium"),
                    InjuryNote(team="国际米兰", player="恰尔汗奥卢", status="out", impact="medium"),
                ],
            },
            {
                "league": "法甲",
                "home": ("巴黎圣日耳曼", "PSG"),
                "away": ("马赛", "MAR"),
                "venue": "王子公园球场",
                "home_form": ["W", "W", "W", "W", "D"],
                "away_form": ["L", "D", "W", "L", "W"],
                "home_ppm": 2.8,
                "away_ppm": 1.2,
                "home_gf": 3.0,
                "away_gf": 1.0,
                "home_ga": 0.4,
                "away_ga": 1.6,
                "weather": "晴, 24°C, 湿度 40%",
                "notes": [
                    "巴黎主场统治力极强，近10主场 9胜1平",
                    "马赛客场表现疲软，近5客场仅1胜",
                    "巴黎三叉戟状态火热，合计进球 45+",
                    "njstats 情报: 巴黎预期进球 (xG) 法甲第一 (2.8)",
                ],
                "injuries": [
                    InjuryNote(team="巴黎圣日耳曼", player="金彭贝", status="out", impact="low"),
                    InjuryNote(team="马赛", player="贡多齐", status="doubtful", impact="medium"),
                ],
            },
        ]

        cfg = demo_configs[demo_index]

        return MatchSnapshot(
            match=MatchRef(
                id=match_id,
                league=cfg["league"],
                kickoff=f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}T19:30:00Z",
                home_team=TeamRef(
                    id=f"nj-h-{demo_index + 1:03d}",
                    name=cfg["home"][0],
                    short_name=cfg["home"][1],
                ),
                away_team=TeamRef(
                    id=f"nj-a-{demo_index + 1:03d}",
                    name=cfg["away"][0],
                    short_name=cfg["away"][1],
                ),
                status="SCHEDULED",
                venue=cfg["venue"],
            ),
            home_form=TeamForm(
                last_5=cfg["home_form"],
                points_per_match=cfg["home_ppm"],
                goals_for_avg=cfg["home_gf"],
                goals_against_avg=cfg["home_ga"],
            ),
            away_form=TeamForm(
                last_5=cfg["away_form"],
                points_per_match=cfg["away_ppm"],
                goals_for_avg=cfg["away_gf"],
                goals_against_avg=cfg["away_ga"],
            ),
            injuries=cfg["injuries"],
            weather=cfg["weather"],
            pre_match_notes=cfg["notes"],
            odds=self._build_demo_odds(match_id),
            provider_evidence=[
                f"provider=njstats",
                "demo_mode=true",
                f"match_id={match_id}",
                "njstats.cn 需要完整认证，当前返回模拟数据",
                "设置 NJSTATS_API_KEY 环境变量可切换为真实 API 模式",
            ],
        )

    def _build_demo_odds(self, match_id: str) -> OddsSnapshot:
        """构建 demo 赔率数据。

        模拟 njstats.cn 可能返回的综合赔率结构。
        """
        # 根据 match_id 变化赔率，使不同比赛有不同数据
        seed = sum(ord(c) for c in match_id) % 10
        base_home = 1.70 + seed * 0.05
        base_draw = 3.40 + seed * 0.03
        base_away = 4.20 - seed * 0.05

        return OddsSnapshot(
            match_id=match_id,
            prices=[
                # 1X2 (欧赔)
                OddsPrice(bookmaker="njstats(demo)", market="1x2", selection="home", line=None, price=round(base_home, 2)),
                OddsPrice(bookmaker="njstats(demo)", market="1x2", selection="draw", line=None, price=round(base_draw, 2)),
                OddsPrice(bookmaker="njstats(demo)", market="1x2", selection="away", line=None, price=round(base_away, 2)),
                # 亚盘
                OddsPrice(bookmaker="njstats(demo)", market="asian_handicap", selection="home", line=-0.5, price=0.92),
                OddsPrice(bookmaker="njstats(demo)", market="asian_handicap", selection="away", line=-0.5, price=0.94),
                # 大小球
                OddsPrice(bookmaker="njstats(demo)", market="over_under", selection="over", line=2.5, price=0.90),
                OddsPrice(bookmaker="njstats(demo)", market="over_under", selection="under", line=2.5, price=0.96),
                # 让球盘 (更深盘口)
                OddsPrice(bookmaker="njstats(demo)", market="asian_handicap", selection="home", line=-1.0, price=1.15),
                OddsPrice(bookmaker="njstats(demo)", market="asian_handicap", selection="away", line=-1.0, price=0.72),
                # 大小球 (其他线)
                OddsPrice(bookmaker="njstats(demo)", market="over_under", selection="over", line=3.0, price=1.20),
                OddsPrice(bookmaker="njstats(demo)", market="over_under", selection="under", line=3.0, price=0.68),
            ],
            last_update=datetime.now(timezone.utc).isoformat(),
            source="njstats-demo",
        )


__all__ = ["NjstatsProvider"]