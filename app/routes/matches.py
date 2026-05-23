from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.models import LiveMatchStatus, MatchRef, MatchSnapshot
from app.services.aggregator import MatchAggregator

router = APIRouter(tags=["matches"])


def get_match_aggregator() -> MatchAggregator:
    try:
        return MatchAggregator(settings=get_settings())
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to initialize match aggregator: {exc}",
        ) from exc


@router.get("/matches/upcoming", response_model=list[MatchRef])
async def get_upcoming_matches(
    date: str | None = None,
    aggregator: MatchAggregator = Depends(get_match_aggregator),
) -> list[MatchRef]:
    try:
        return await aggregator.get_upcoming_matches(date=date)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch upcoming matches: {exc}",
        ) from exc


@router.get("/match/{match_id}/snapshot", response_model=MatchSnapshot)
async def get_match_snapshot(
    match_id: str,
    aggregator: MatchAggregator = Depends(get_match_aggregator),
) -> MatchSnapshot:
    try:
        return await aggregator.get_match_snapshot(match_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch match snapshot for '{match_id}': {exc}",
        ) from exc


# ── 新增端点 ──────────────────────────────────────────────


def _is_live(match: MatchRef) -> bool:
    """判断比赛是否处于进行中状态（含中场休息）。"""
    status = (match.live_status or match.status or "").upper()
    return status in ("LIVE", "HT")


def _is_finished(match: MatchRef) -> bool:
    """判断比赛是否已结束。"""
    status = (match.live_status or match.status or "").upper()
    return status in ("FT", "FINISHED")


def _is_scheduled(match: MatchRef) -> bool:
    """判断比赛是否为未开赛。"""
    status = (match.live_status or match.status or "").upper()
    return status in ("SCHEDULED", "NS")


def _match_to_live_status(match: MatchRef) -> LiveMatchStatus:
    """将 MatchRef 转换为 LiveMatchStatus。"""
    return LiveMatchStatus(
        match_id=match.id,
        league=match.league,
        home_team=match.home_team.name,
        away_team=match.away_team.name,
        home_score=match.home_score or 0,
        away_score=match.away_score or 0,
        minute=match.minute or 0,
        status=(match.live_status or match.status or "LIVE").upper(),
        kickoff=match.kickoff,
    )


@router.get("/matches/live", response_model=list[LiveMatchStatus])
async def get_live_matches(
    aggregator: MatchAggregator = Depends(get_match_aggregator),
) -> list[LiveMatchStatus]:
    """返回所有进行中比赛的实时状态列表。"""
    try:
        all_matches = await aggregator.get_upcoming_matches()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch matches: {exc}",
        ) from exc

    live_matches = [m for m in all_matches if _is_live(m)]
    return [_match_to_live_status(m) for m in live_matches]


@router.get("/matches/status", response_model=list[MatchRef])
async def get_matches_by_status(
    filter: str = Query("all", description="筛选条件: live / finished / scheduled / all"),
    aggregator: MatchAggregator = Depends(get_match_aggregator),
) -> list[MatchRef]:
    """按状态筛选比赛列表。"""
    try:
        all_matches = await aggregator.get_upcoming_matches()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch matches: {exc}",
        ) from exc

    filter_lower = filter.strip().lower()

    if filter_lower == "live":
        return [m for m in all_matches if _is_live(m)]
    elif filter_lower == "finished":
        return [m for m in all_matches if _is_finished(m)]
    elif filter_lower == "scheduled":
        return [m for m in all_matches if _is_scheduled(m)]
    else:
        # "all" 或未知值均返回全部
        return all_matches
