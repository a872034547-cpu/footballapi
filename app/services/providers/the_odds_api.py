from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.models import MatchRef, MatchSnapshot, OddsPrice, OddsSnapshot
from app.services.providers.base import BaseProvider


MARKET_NAME_MAP: dict[str, str] = {
    "h2h": "1X2",
    "spreads": "asian_handicap",
    "totals": "over_under",
}


class TheOddsApiProvider(BaseProvider):
    """The Odds API provider.

    当前实现遵循以下原则：
    - 比赛列表与基础快照优先返回 demo 数据，保证上层聚合器稳定可用
    - 赔率在 demo/no key/远端失败时自动回退到 base provider 的 demo odds
    - 若远端可用，则按 The Odds API 常见结构解析 bookmakers/markets/outcomes
    """

    name = "the_odds"

    def __init__(self, settings: Any | None = None) -> None:
        super().__init__(settings or get_settings())

    def is_configured(self) -> bool:
        return bool(self.settings.demo_mode or self.settings.the_odds_api_key)

    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        return []

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        snapshot = self.build_demo_snapshot(match_id)
        snapshot.odds = await self.get_match_odds(match_id)
        return snapshot

    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        fallback = self._build_demo_odds_snapshot(match_id)

        if self.settings.demo_mode or not self.settings.the_odds_api_key:
            return fallback

        try:
            event = await self._fetch_event(match_id)
            if not event:
                return fallback

            snapshot = self._parse_event_odds(match_id=match_id, event=event)
            return snapshot if snapshot.prices else fallback
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return fallback

    async def _fetch_event(self, match_id: str) -> dict[str, Any] | None:
        params = {
            "apiKey": self.settings.the_odds_api_key,
            "regions": "eu,uk,us,au",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "eventIds": match_id,
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(self._odds_url, params=params)
            response.raise_for_status()
            payload = response.json()

        return self._extract_event(payload=payload, match_id=match_id)

    def _extract_event(self, payload: Any, match_id: str) -> dict[str, Any] | None:
        events = self._coerce_events(payload)

        for event in events:
            if str(event.get("id", "")) == str(match_id):
                return event

        if len(events) == 1:
            return events[0]

        return None

    def _coerce_events(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            if payload.get("id") is not None:
                return [payload]

            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                return [data]

        return []

    def _parse_event_odds(self, match_id: str, event: dict[str, Any]) -> OddsSnapshot:
        home_team = str(event.get("home_team") or "")
        away_team = str(event.get("away_team") or "")
        prices: list[OddsPrice] = []
        last_update: str | None = None

        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            bookmakers = []

        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue

            bookmaker_name = str(bookmaker.get("title") or bookmaker.get("key") or "unknown-bookmaker")
            bookmaker_update = bookmaker.get("last_update")
            if isinstance(bookmaker_update, str) and bookmaker_update:
                last_update = bookmaker_update

            markets = bookmaker.get("markets")
            if not isinstance(markets, list):
                continue

            for market in markets:
                if not isinstance(market, dict):
                    continue

                market_key = str(market.get("key") or market.get("market_key") or "").strip().lower()
                mapped_market = MARKET_NAME_MAP.get(market_key)
                if not mapped_market:
                    continue

                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    continue

                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue

                    selection = self._map_selection(
                        market_key=market_key,
                        outcome=outcome,
                        home_team=home_team,
                        away_team=away_team,
                    )
                    if not selection:
                        continue

                    price = self._safe_float(outcome.get("price"), default=0.0)
                    if price <= 0:
                        continue

                    prices.append(
                        OddsPrice(
                            bookmaker=bookmaker_name,
                            market=mapped_market,
                            selection=selection,
                            line=self._extract_line(outcome),
                            price=price,
                        )
                    )

        if not last_update:
            candidate = event.get("last_update") or event.get("commence_time")
            last_update = str(candidate) if candidate else None

        return OddsSnapshot(
            match_id=match_id,
            prices=prices,
            last_update=last_update,
            source=self.name,
        )

    def _map_selection(
        self,
        market_key: str,
        outcome: dict[str, Any],
        home_team: str,
        away_team: str,
    ) -> str:
        raw_name = str(outcome.get("name") or "").strip()
        normalized_name = self._normalize_text(raw_name)
        normalized_home = self._normalize_text(home_team)
        normalized_away = self._normalize_text(away_team)

        if market_key == "h2h":
            if normalized_name in {"draw", "tie", "x"}:
                return "draw"
            if normalized_name in {"home", normalized_home}:
                return "home"
            if normalized_name in {"away", normalized_away}:
                return "away"
            return raw_name or normalized_name

        if market_key == "spreads":
            if normalized_name in {"home", normalized_home}:
                return "home"
            if normalized_name in {"away", normalized_away}:
                return "away"
            return raw_name or normalized_name

        if market_key == "totals":
            if normalized_name.startswith("over"):
                return "over"
            if normalized_name.startswith("under"):
                return "under"
            return raw_name or normalized_name

        return raw_name or normalized_name

    def _extract_line(self, outcome: dict[str, Any]) -> float | None:
        point = outcome.get("point")
        if point is None or point == "":
            return None

        try:
            return float(point)
        except (TypeError, ValueError):
            return None

    def _normalize_text(self, value: str) -> str:
        return " ".join(value.lower().split())

    def _build_demo_odds_snapshot(self, match_id: str) -> OddsSnapshot:
        demo_snapshot = self.build_demo_snapshot(match_id)
        if demo_snapshot.odds is not None:
            return demo_snapshot.odds

        return OddsSnapshot(
            match_id=match_id,
            prices=[],
            last_update=None,
            source=self.name,
        )

    @property
    def _odds_url(self) -> str:
        return f"{str(self.settings.the_odds_base_url).rstrip('/')}/sports/upcoming/odds"


__all__ = ["TheOddsApiProvider"]
