from __future__ import annotations

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

        for provider in (self.football_data, self.the_odds, self.goalserve, self.wubai, self.zgzcw, self.njstats, self.nowscore):
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

        return list(merged.values())

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        provider_errors: list[str] = []

        try:
            snapshot = await self.football_data.get_match_snapshot(match_id)
        except Exception as exc:
            provider_errors.append(f"football_data:{exc.__class__.__name__}")
            snapshot = self.football_data.build_demo_snapshot(match_id)

        base_odds = snapshot.odds
        merged_prices = []
        odds_sources: list[str] = []
        last_update: str | None = None

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
