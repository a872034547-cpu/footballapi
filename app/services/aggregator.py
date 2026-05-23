from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.models import MatchRef, MatchSnapshot, OddsSnapshot, SourceStatus
from app.services.providers.football_data import FootballDataProvider
from app.services.providers.goalserve import GoalserveProvider
from app.services.providers.njstats import NjstatsProvider
from app.services.providers.nowscore import NowscoreProvider
from app.services.providers.the_odds_api import TheOddsApiProvider
from app.services.providers.wubai import WubaiProvider
from app.services.providers.zgzcw import ZgzcwProvider


class MatchAggregator:
    """统一聚合基础比赛数据与多来源赔率数据。"""

    def __init__(self, settings: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.football_data = FootballDataProvider(self.settings)
        self.the_odds = TheOddsApiProvider(self.settings)
        self.goalserve = GoalserveProvider(self.settings)
        self.wubai = WubaiProvider(self.settings)
        self.zgzcw = ZgzcwProvider(self.settings)
        self.njstats = NjstatsProvider(self.settings)
        self.nowscore = NowscoreProvider(self.settings)
        self.providers = {
            "football_data": self.football_data,
            "the_odds": self.the_odds,
            "goalserve": self.goalserve,
            "wubai": self.wubai,
            "zgzcw": self.zgzcw,
            "njstats": self.njstats,
            "nowscore": self.nowscore,
        }

    def describe_sources(self) -> list[SourceStatus]:
        enabled_sources = getattr(self.settings, "enabled_sources", {})
        demo_mode = bool(getattr(self.settings, "demo_mode", False))
        sources: list[SourceStatus] = []

        for name, provider in self.providers.items():
            configured = provider.is_configured()
            enabled = bool(enabled_sources.get(name, configured))
            if demo_mode:
                detail = "demo mode enabled"
            elif configured:
                detail = "configured"
            else:
                detail = "missing api key"

            sources.append(
                SourceStatus(
                    name=name,
                    enabled=enabled,
                    configured=configured,
                    detail=detail,
                )
            )

        return sources

    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        merged: dict[str, MatchRef] = {}
        nowscore_matches: list[MatchRef] = []

        # nowscore 优先：它有实时比分/状态；zgzcw 其次：它有最全的赛程列表
        for provider in (self.nowscore, self.zgzcw, self.wubai, self.football_data, self.the_odds, self.goalserve, self.njstats):
            try:
                matches = await provider.get_upcoming_matches(date=date)
            except Exception:
                continue

            for item in matches:
                match = item.match if isinstance(item, MatchSnapshot) else item
                if not isinstance(match, MatchRef):
                    continue
                key = str(match.id).strip()
                if key and key not in merged:
                    merged[key] = match
                    if provider is self.nowscore:
                        nowscore_matches.append(match)

        # ── 跨源状态合并 ──────────────────────────────────────
        # nowscore 使用 "nowscore-{id}" 前缀 ID，zgzcw 使用纯数字 ID，
        # 两者按 ID 永远无法匹配。通过 (联赛, 主队名, 客队名) 三元组
        # 将 nowscore 的实时状态/比分/分钟数覆盖到 zgzcw 比赛上。
        if nowscore_matches:
            ns_index: dict[tuple[str, str, str], MatchRef] = {}
            for ns in nowscore_matches:
                ns_key = (
                    (ns.league or "").strip().lower(),
                    (ns.home_team.name or "").strip().lower(),
                    (ns.away_team.name or "").strip().lower(),
                )
                ns_index[ns_key] = ns

            for key, match in list(merged.items()):
                if key.startswith("nowscore-"):
                    continue
                if match.status and match.status != "SCHEDULED":
                    continue

                match_key = (
                    (match.league or "").strip().lower(),
                    (match.home_team.name or "").strip().lower(),
                    (match.away_team.name or "").strip().lower(),
                )

                ns_match = ns_index.get(match_key)
                if ns_match and ns_match.status and ns_match.status != "SCHEDULED":
                    match.status = ns_match.status
                    match.home_score = ns_match.home_score
                    match.away_score = ns_match.away_score
                    match.minute = ns_match.minute
                    match.live_status = ns_match.live_status

        # ── 基于时间的状态推断（兜底）────────────────────────
        # 当所有 provider 都只返回 SCHEDULED 时，根据开赛时间推断实际状态。
        # 足球比赛通常 90 分钟 + 中场 15 分钟 + 补时 ≈ 120 分钟。
        # 注意：zgzcw 返回的 kickoff 是北京时间 (UTC+8)，需要转换。
        from datetime import timedelta
        CST = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        for match in merged.values():
            if match.status and match.status != "SCHEDULED":
                continue
            if not match.kickoff:
                continue
            try:
                # 尝试多种日期格式（kickoff 是北京时间）
                kt_cst = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
                    try:
                        kt_cst = datetime.strptime(match.kickoff[:19], fmt).replace(tzinfo=CST)
                        break
                    except ValueError:
                        continue
                if kt_cst is None:
                    continue

                # 转为 UTC 进行比较
                kt_utc = kt_cst.astimezone(timezone.utc)
                elapsed = (now_utc - kt_utc).total_seconds()
                if elapsed > 150 * 60:  # 开赛超过 2.5 小时 → 已结束
                    match.status = "FINISHED"
                    match.live_status = "FINISHED"
                elif elapsed > 0:  # 已开赛但未超过 2.5 小时 → 进行中
                    match.status = "LIVE"
                    match.live_status = "LIVE"
                    match.minute = min(int(elapsed // 60), 120)
            except Exception:
                continue

        return list(merged.values())

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        provider_errors: list[str] = []
        snapshot: MatchSnapshot | None = None

        # ── 优先使用有真实数据的 provider 获取 snapshot ──
        # zgzcw / nowscore / wubai 是逆向抓取器，能拿到真实比赛数据
        # football_data / the_odds / goalserve 需要 API key，demo 模式下返回假数据
        real_providers = (self.zgzcw, self.nowscore, self.wubai)
        for provider in real_providers:
            try:
                snapshot = await provider.get_match_snapshot(match_id)
                if snapshot is not None and snapshot.match.home_team.name:
                    break
            except Exception as exc:
                provider_errors.append(f"{provider.name}:{exc.__class__.__name__}")
                continue

        # ── 回退：football_data（可能是 demo 数据）──
        if snapshot is None:
            try:
                snapshot = await self.football_data.get_match_snapshot(match_id)
            except Exception as exc:
                provider_errors.append(f"football_data:{exc.__class__.__name__}")
                snapshot = self.football_data.build_demo_snapshot(match_id)

        base_odds = snapshot.odds
        merged_prices = []
        odds_sources: list[str] = []
        last_update: str | None = None

        # ── 合并所有 provider 的赔率数据 ──
        for provider in (self.the_odds, self.goalserve, self.wubai, self.zgzcw, self.njstats, self.nowscore):
            try:
                odds = await provider.get_match_odds(match_id)
            except Exception as exc:
                provider_errors.append(f"{provider.name}:{exc.__class__.__name__}")
                continue

            if not isinstance(odds, OddsSnapshot):
                continue

            if odds.prices:
                merged_prices.extend(odds.prices)
            if odds.source:
                odds_sources.append(odds.source)
            if odds.last_update and (last_update is None or odds.last_update > last_update):
                last_update = odds.last_update

        if not merged_prices and base_odds is not None:
            merged_prices = list(base_odds.prices)
            last_update = base_odds.last_update
            if base_odds.source:
                odds_sources.append(base_odds.source)

        snapshot.odds = OddsSnapshot(
            match_id=match_id,
            prices=merged_prices,
            last_update=last_update,
            source="aggregated",
        )

        evidence = list(snapshot.provider_evidence)
        evidence.extend(f"odds_source:{name}" for name in odds_sources if name)
        evidence.extend(f"provider_error:{value}" for value in provider_errors)
        snapshot.provider_evidence = list(dict.fromkeys(evidence))

        notes = list(snapshot.pre_match_notes)
        if merged_prices:
            notes.append("Odds merged from normalized provider snapshots.")
        if provider_errors:
            notes.append("Some providers failed and were replaced with fallback data.")
        snapshot.pre_match_notes = list(dict.fromkeys(notes))

        return snapshot


__all__ = ["MatchAggregator"]
