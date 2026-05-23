from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.models import (
    AnalysisResponse,
    KellyResult,
    MatchRef,
    MatchSnapshot,
    PredictionRequest,
    RollingPrediction,
    TeamForm,
    TeamRef,
)
from app.services.aggregator import MatchAggregator
from app.services.kelly_calculator import KellyCalculator
from app.services.pattern_predictor import PatternPredictor
from app.services.predictor import RuleBasedPredictor

router = APIRouter(tags=["predict"])


def get_match_aggregator() -> MatchAggregator:
    return MatchAggregator(get_settings())


def get_predictor() -> RuleBasedPredictor:
    return RuleBasedPredictor()


def get_pattern_predictor() -> PatternPredictor:
    return PatternPredictor()


def _normalize_form_results(results: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in results[:5]:
        token = value.strip().upper()
        if not token:
            continue
        if token.startswith("W"):
            normalized.append("W")
        elif token.startswith("D"):
            normalized.append("D")
        elif token.startswith("L"):
            normalized.append("L")
    return normalized


def _to_team_form(results: list[str]) -> TeamForm:
    normalized = _normalize_form_results(results)
    sample_size = len(normalized)
    if sample_size == 0:
        return TeamForm()

    wins = sum(1 for result in normalized if result == "W")
    draws = sum(1 for result in normalized if result == "D")
    losses = sum(1 for result in normalized if result == "L")

    points_per_match = (wins * 3 + draws) / sample_size
    goals_for_avg = max(0.2, 1.1 + wins * 0.18 - losses * 0.08)
    goals_against_avg = max(0.2, 1.1 + losses * 0.18 - wins * 0.08)

    return TeamForm(
        last_5=normalized,
        points_per_match=round(points_per_match, 2),
        goals_for_avg=round(goals_for_avg, 2),
        goals_against_avg=round(goals_against_avg, 2),
    )


def _build_manual_snapshot(request: PredictionRequest) -> MatchSnapshot:
    home_team_name = (request.home_team or "Home").strip() or "Home"
    away_team_name = (request.away_team or "Away").strip() or "Away"
    match_id = request.match_id or "manual-prediction"

    pre_match_notes = [
        "Snapshot generated from manual prediction request.",
        f"Requested over/under line: {request.over_under_line}",
        f"Requested handicap line: {request.handicap_line}",
    ]
    if not request.injuries:
        pre_match_notes.append("No injury data supplied in request.")

    return MatchSnapshot(
        match=MatchRef(
            id=match_id,
            league=request.league or "Custom",
            kickoff=request.kickoff or "",
            home_team=TeamRef(id="home", name=home_team_name),
            away_team=TeamRef(id="away", name=away_team_name),
            status="SCHEDULED",
        ),
        home_form=_to_team_form(request.home_recent_form),
        away_form=_to_team_form(request.away_recent_form),
        injuries=request.injuries,
        pre_match_notes=pre_match_notes,
        provider_evidence=[
            "source:manual_request",
            "provider:none",
            f"match_id:{match_id}",
        ],
    )


@router.get("/match/{match_id}/analysis", response_model=AnalysisResponse)
async def get_match_analysis(
    match_id: str,
    aggregator: MatchAggregator = Depends(get_match_aggregator),
    predictor: RuleBasedPredictor = Depends(get_predictor),
) -> AnalysisResponse:
    try:
        # 只读取已缓存的比赛身份，不为单场分析主动刷新全量赛程。
        # 这样 Match ID 获取与分析接口解耦，避免一次分析触发多源列表抓取。
        cached_match = aggregator.get_cached_match_ref(match_id)
        snapshot = await aggregator.get_match_snapshot(match_id)

        # 用已知真实身份覆盖 fallback/demo snapshot，但不触发额外外部请求。
        if cached_match is not None:
            snapshot.match = cached_match

        return predictor.analyze_snapshot(snapshot)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to analyze match: {exc}") from exc


@router.post("/predict", response_model=AnalysisResponse)
async def predict_match(
    request: PredictionRequest,
    aggregator: MatchAggregator = Depends(get_match_aggregator),
    predictor: RuleBasedPredictor = Depends(get_predictor),
) -> AnalysisResponse:
    try:
        if request.match_id:
            snapshot = await aggregator.get_match_snapshot(request.match_id)
        else:
            snapshot = _build_manual_snapshot(request)
        return predictor.analyze_snapshot(snapshot)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to generate prediction: {exc}") from exc


# ── 凯利指数端点 ──────────────────────────────────────────


@router.get("/match/{match_id}/kelly", response_model=KellyResult)
async def get_match_kelly(
    match_id: str,
    aggregator: MatchAggregator = Depends(get_match_aggregator),
) -> KellyResult:
    """计算比赛的凯利指数。

    从所有已启用的数据源收集赔率数据，
    按博彩公司分组计算各市场的凯利指数。

    凯利值 > 1: 庄家高估该结果（对庄家有利）
    凯利值 < 1: 庄家低估该结果（对玩家有利）
    """
    try:
        snapshot = await aggregator.get_match_snapshot(match_id)
        odds = snapshot.odds
        if not odds or not odds.prices:
            return KellyResult(match_id=match_id, bookmaker_count=0)

        calculator = KellyCalculator()
        return calculator.calculate(match_id, [odds])
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to calculate kelly index: {exc}",
        ) from exc


# ── 滚球预测端点 ──────────────────────────────────────────


@router.get("/match/{match_id}/rolling", response_model=list[RollingPrediction])
async def get_rolling_prediction(
    match_id: str,
    minute: int = Query(0, description="当前比赛分钟数"),
    home_score: int = Query(0, description="主队当前进球"),
    away_score: int = Query(0, description="客队当前进球"),
    aggregator: MatchAggregator = Depends(get_match_aggregator),
    pattern_predictor: PatternPredictor = Depends(get_pattern_predictor),
) -> list[RollingPrediction]:
    """基于当前比分和时间生成滚球预测。

    查询参数：
    - minute: 当前比赛分钟数（默认 0）
    - home_score: 主队当前进球（默认 0）
    - away_score: 客队当前进球（默认 0）

    返回滚球预测列表，包含：
    - 剩余时间胜平负概率
    - 大小球调整建议
    - 下一进球方预测
    """
    try:
        snapshot = await aggregator.get_match_snapshot(match_id)
        return pattern_predictor.build_rolling_prediction(
            snapshot,
            current_minute=minute,
            home_score=home_score,
            away_score=away_score,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to generate rolling prediction: {exc}",
        ) from exc
