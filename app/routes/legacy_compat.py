"""兼容 C:\0521\FootballPredictSite PHP 项目的旧版 API 契约。

PHP 项目通过 ExistingApiSource 适配器调用以下 4 个端点：
  GET  /football/matches?type=all   → 比赛列表
  GET  /analysis/info?fid={fid}     → 单场信息（含 is_redis 标记）
  POST /analysis/all                → 完整分析数据
  GET  /football/daily/{date}       → 按日期筛选比赛

所有响应统一包裹为 {"code": 200, "data": ...}。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.models import (
    AnalysisResponse,
    MatchRef,
    MatchSnapshot,
    OddsPrice,
    OddsSnapshot,
    ScorePrediction,
)
from app.services.aggregator import MatchAggregator
from app.services.predictor import RuleBasedPredictor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["legacy-compat"])

_aggregator: MatchAggregator | None = None
_predictor: RuleBasedPredictor | None = None


def _get_aggregator() -> MatchAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = MatchAggregator()
    return _aggregator


def _get_predictor() -> RuleBasedPredictor:
    global _predictor
    if _predictor is None:
        _predictor = RuleBasedPredictor()
    return _predictor


# ---------------------------------------------------------------------------
# 响应包装
# ---------------------------------------------------------------------------

def _ok(data: Any) -> dict[str, Any]:
    return {"code": 200, "data": data}


def _err(code: int, msg: str) -> dict[str, Any]:
    return {"code": code, "msg": msg}


# ---------------------------------------------------------------------------
# 字段映射：MatchRef → PHP 期望的扁平字典
# ---------------------------------------------------------------------------

def _match_to_legacy(m: MatchRef) -> dict[str, Any]:
    """将内部 MatchRef 映射为 PHP normalizeMatch() 能识别的字段。"""
    return {
        "fid": str(m.id),
        "id": str(m.id),
        "home_team": m.home_team.name,
        "home_name": m.home_team.name,
        "away_team": m.away_team.name,
        "visit_team": m.away_team.name,
        "away_name": m.away_team.name,
        "league": m.league,
        "match_group": m.league,
        "match_category": m.league,
        "match_time": m.kickoff,
        "time": m.kickoff,
        "running_time": m.kickoff,
        "match_status": m.status,
        "status": m.status,
        "home_score": None,
        "visit_score": None,
        "away_score": None,
        "field_score": None,
        "score": None,
        "score_str": None,
        "venue": m.venue,
    }


# ---------------------------------------------------------------------------
# 赔率映射：OddsPrice → PHP 期望的 odds_items 条目
# ---------------------------------------------------------------------------

def _odds_to_legacy_items(prices: list[OddsPrice]) -> dict[str, list[dict[str, Any]]]:
    """将 OddsPrice 列表按 market 分组为 europe_odds_items / asia_odds_items / size_odds_items。"""
    europe: list[dict[str, Any]] = []
    asia: list[dict[str, Any]] = []
    size: list[dict[str, Any]] = []

    for p in prices:
        entry: dict[str, Any] = {
            "bookmaker": p.bookmaker,
            "market": p.market,
            "selection": p.selection,
            "line": p.line,
            "price": p.price,
        }
        market_lower = p.market.lower()
        if "1x2" in market_lower or "h2h" in market_lower or "match_odds" in market_lower or "europe" in market_lower:
            europe.append(entry)
        elif "asian" in market_lower or "handicap" in market_lower or "spread" in market_lower:
            asia.append(entry)
        elif "total" in market_lower or "over_under" in market_lower or "goals" in market_lower or "size" in market_lower:
            size.append(entry)
        else:
            # 无法归类时放入 europe
            europe.append(entry)

    return {
        "europe_odds_items": europe,
        "asia_odds_items": asia,
        "size_odds_items": size,
    }


def _derive_pan_most(odds: OddsSnapshot | None) -> dict[str, Any]:
    """从赔率数据推导 instant_pan_most / origin_pan_most 等字段。"""
    result: dict[str, Any] = {
        "instant_pan_most": "",
        "origin_pan_most": "",
        "instant_size_most": "",
        "origin_size_most": "",
    }
    if odds is None or not odds.prices:
        return result

    # 统计最常见的 handicap line
    handicap_lines: dict[float, int] = {}
    size_lines: dict[float, int] = {}
    for p in odds.prices:
        if p.line is None:
            continue
        market_lower = p.market.lower()
        if "asian" in market_lower or "handicap" in market_lower or "spread" in market_lower:
            handicap_lines[p.line] = handicap_lines.get(p.line, 0) + 1
        elif "total" in market_lower or "over_under" in market_lower or "goals" in market_lower:
            size_lines[p.line] = size_lines.get(p.line, 0) + 1

    if handicap_lines:
        most = max(handicap_lines, key=lambda k: handicap_lines[k])
        result["instant_pan_most"] = str(most)
        result["origin_pan_most"] = str(most)
    if size_lines:
        most = max(size_lines, key=lambda k: size_lines[k])
        result["instant_size_most"] = str(most)
        result["origin_size_most"] = str(most)

    return result


# ---------------------------------------------------------------------------
# 分析结果映射：AnalysisResponse → PHP hasComputedAnalysisData 期望的字段
# ---------------------------------------------------------------------------

def _analysis_to_legacy_computed(
    analysis: AnalysisResponse,
    snapshot: MatchSnapshot,
) -> dict[str, Any]:
    """将 AnalysisResponse 映射为 PHP hasComputedAnalysisData() 能识别的字段。

    预测器标签格式（概率已是 0-100 百分比）：
      win_draw_lose: "home_win" / "draw" / "away_win"
      over_under:    "over_2.5" / "under_2.5"
      asian_handicap: "home_-0.5" / "away_0.0" / "home_0.0" 等动态标签
    """
    result: dict[str, Any] = {}

    # --- 欧赔胜平负概率 ---
    wdl = {item.label: item.probability for item in analysis.win_draw_lose}
    result["europe_win_all"] = round(wdl.get("home_win", 0.0), 4)
    result["europe_even_all"] = round(wdl.get("draw", 0.0), 4)
    result["europe_lose_all"] = round(wdl.get("away_win", 0.0), 4)

    # --- 亚盘：从标签中提取主/客概率 ---
    # 标签格式如 "home_-0.5" (主队让球) 或 "away_0.0" (客队受让)
    ah_home = 0.0
    ah_away = 0.0
    for item in analysis.asian_handicap:
        if item.label.startswith("home_"):
            ah_home = item.probability
        elif item.label.startswith("away_"):
            ah_away = item.probability
    result["asia_win_all"] = round(ah_home, 4)
    result["asia_lose_all"] = round(ah_away, 4)
    result["asia_run_all"] = round(max(0.0, 100.0 - ah_home - ah_away), 4)

    # --- 大小球 ---
    ou = {item.label: item.probability for item in analysis.over_under}
    result["size_big_all"] = round(ou.get("over_2.5", 0.0), 4)
    result["size_small_all"] = round(ou.get("under_2.5", 0.0), 4)
    result["size_run_all"] = round(max(0.0, 100.0 - result["size_big_all"] - result["size_small_all"]), 4)

    # --- 比分列表 ---
    result["europe_score_list"] = [
        {"score": s.score, "probability": round(s.probability, 4)}
        for s in analysis.predicted_scores
    ]
    result["asia_score_list"] = result["europe_score_list"]
    result["size_score_list"] = result["europe_score_list"]

    # --- 进球数列表 ---
    result["goal_number_list"] = _build_goal_number_list(analysis)

    # --- 推理数据 ---
    result["infer_data"] = {
        "confidence": analysis.confidence_score,
        "benefit_factors": analysis.benefit_factors,
        "risk_factors": analysis.risk_factors,
        "pre_match_notes": analysis.pre_match_notes,
    }

    # --- 泊松相关 ---
    result["poisson_small"] = result["size_small_all"]
    result["poisson_big"] = result["size_big_all"]
    result["poisson_small_limit"] = 2.5
    result["poisson_big_limit"] = 2.5

    # --- 联赛级统计（暂无，填 0） ---
    for key in (
        "europe_win_league", "europe_even_league", "europe_lose_league",
        "asia_win_league", "asia_run_league", "asia_lose_league",
        "size_big_league", "size_run_league", "size_small_league",
    ):
        result[key] = 0.0

    result["europe_matches"] = 0
    result["asia_matches"] = 0
    result["size_matches"] = 0
    result["infer_score"] = ""
    result["instant_infer_average"] = ""
    result["origin_infer_average"] = ""
    result["half_goal_number_list"] = []

    return result


def _build_goal_number_list(analysis: AnalysisResponse) -> list[dict[str, Any]]:
    """从预测比分推导进球数分布。"""
    goal_counts: dict[int, float] = {}
    for s in analysis.predicted_scores:
        try:
            parts = s.score.split("-")
            if len(parts) == 2:
                total = int(parts[0]) + int(parts[1])
                goal_counts[total] = goal_counts.get(total, 0.0) + s.probability
        except (ValueError, IndexError):
            continue

    total_prob = sum(goal_counts.values())
    if total_prob > 0:
        return [
            {"goals": g, "probability": round(p / total_prob, 4)}
            for g, p in sorted(goal_counts.items())
        ]
    return []


# ===========================================================================
# 端点实现
# ===========================================================================


@router.get("/football/matches")
async def legacy_get_matches(
    type: str = Query("all"),
    _t: int = Query(0),
):
    """返回比赛列表，兼容 PHP ExistingApiSource::getMatches()。"""
    try:
        agg = _get_aggregator()
        matches = await agg.get_upcoming_matches()
        data = [_match_to_legacy(m) for m in matches]
        return _ok(data)
    except Exception as exc:
        logger.exception("legacy_get_matches failed")
        return _err(500, str(exc))


@router.get("/analysis/info")
async def legacy_get_analysis_info(
    fid: str = Query(...),
):
    """返回单场比赛信息 + 完整分析数据。

    设置 is_redis=True 并附带 hasComputedAnalysisData 所需字段，
    使 PHP analysisMatch() 在第一步就拿到完整数据直接返回，
    避免额外的 POST /analysis/all 调用。
    """
    try:
        agg = _get_aggregator()
        predictor = _get_predictor()

        snapshot = await agg.get_match_snapshot(fid)
        analysis = predictor.analyze_snapshot(snapshot)

        # 基础比赛信息
        base = _match_to_legacy(snapshot.match)

        # 赔率 items
        if snapshot.odds and snapshot.odds.prices:
            odds_items = _odds_to_legacy_items(snapshot.odds.prices)
        else:
            odds_items = {"europe_odds_items": [], "asia_odds_items": [], "size_odds_items": []}

        # 盘口 most
        pan_most = _derive_pan_most(snapshot.odds)

        # 计算分析数据
        computed = _analysis_to_legacy_computed(analysis, snapshot)

        # 合并
        data: dict[str, Any] = {
            "is_redis": True,
            **base,
            **odds_items,
            **pan_most,
            **computed,
            # 额外字段
            "home_team_rank": "",
            "visit_team_rank": "",
            "match_round": "",
            "match_type": "",
            "match_url": "",
            "europe_companies": [],
            "asia_companies": [],
            "size_companies": [],
            "asia_compose_size": "",
            "size_compose_asia": "",
            "asia_nonMainstream": "",
            "size_nonMainstream": "",
            "no_friend_match": "",
            "asia_filter_odds": "",
            "size_filter_odds": "",
            "only_main_match": "",
        }
        return _ok(data)
    except Exception as exc:
        logger.exception("legacy_get_analysis_info failed for fid=%s", fid)
        return _err(500, str(exc))


@router.post("/analysis/all")
async def legacy_post_analysis_all(request: Request):
    """返回完整分析数据。

    PHP analysisMatch() 在 GET /analysis/info 未返回 is_redis 时
    会 fallback 到本端点。我们直接返回与 /analysis/info 相同结构的数据。
    """
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            fid = str(payload.get("fid", payload.get("id", "")))
        else:
            fid = ""

        if not fid:
            return _err(400, "missing fid")

        agg = _get_aggregator()
        predictor = _get_predictor()

        snapshot = await agg.get_match_snapshot(fid)
        analysis = predictor.analyze_snapshot(snapshot)

        base = _match_to_legacy(snapshot.match)

        if snapshot.odds and snapshot.odds.prices:
            odds_items = _odds_to_legacy_items(snapshot.odds.prices)
        else:
            odds_items = {"europe_odds_items": [], "asia_odds_items": [], "size_odds_items": []}

        pan_most = _derive_pan_most(snapshot.odds)
        computed = _analysis_to_legacy_computed(analysis, snapshot)

        data: dict[str, Any] = {
            "is_redis": True,
            **base,
            **odds_items,
            **pan_most,
            **computed,
            "home_team_rank": "",
            "visit_team_rank": "",
            "match_round": "",
            "match_type": "",
            "match_url": "",
            "europe_companies": [],
            "asia_companies": [],
            "size_companies": [],
            "asia_compose_size": "",
            "size_compose_asia": "",
            "asia_nonMainstream": "",
            "size_nonMainstream": "",
            "no_friend_match": "",
            "asia_filter_odds": "",
            "size_filter_odds": "",
            "only_main_match": "",
        }
        return _ok(data)
    except Exception as exc:
        logger.exception("legacy_post_analysis_all failed")
        return _err(500, str(exc))


@router.get("/football/daily/{date_str}")
async def legacy_get_daily_matches(
    date_str: str,
    pageNum: int = Query(1),
    pageSize: int = Query(200),
):
    """按日期返回比赛列表。

    PHP getDailyMatches() 期望 data.data 或 data.records 或 data.list。
    """
    try:
        agg = _get_aggregator()
        matches = await agg.get_upcoming_matches(date=date_str)

        # 如果聚合器不支持 date 过滤，则在本地按 kickoff 日期过滤
        filtered: list[dict[str, Any]] = []
        for m in matches:
            legacy = _match_to_legacy(m)
            kickoff = legacy.get("match_time", "")
            if date_str in kickoff:
                filtered.append(legacy)

        if not filtered:
            # 无过滤结果时返回全部（PHP 端会自行处理）
            filtered = [_match_to_legacy(m) for m in matches]

        # 分页
        start = (pageNum - 1) * pageSize
        end = start + pageSize
        page_data = filtered[start:end]

        return _ok({
            "data": page_data,
            "records": page_data,
            "list": page_data,
            "total": len(filtered),
            "pageNum": pageNum,
            "pageSize": pageSize,
        })
    except Exception as exc:
        logger.exception("legacy_get_daily_matches failed for date=%s", date_str)
        return _err(500, str(exc))