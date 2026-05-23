from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.models import MatchRef, MatchSnapshot, OddsPrice, OddsSnapshot
from app.services.providers.base import BaseProvider


MARKET_NAME_MAP: dict[str, str] = {
    "1x2": "1X2",
    "h2h": "1X2",
    "match_winner": "1X2",
    "spreads": "asian_handicap",
    "asian_handicap": "asian_handicap",
    "totals": "over_under",
    "over_under": "over_under",
}


class GoalserveProvider(BaseProvider):
    """Goalserve 赔率提供方。"""

    name = "goalserve"

    def __init__(self, settings: Any | None = None) -> None:
        super().__init__(settings or get_settings())

    def is_configured(self) -> bool:
        return bool(self.settings.demo_mode or self.settings.goalserve_api_key)

    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        return []

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        snapshot = self.build_demo_snapshot(match_id)
        snapshot.odds = await self.get_match_odds(match_id)
        snapshot.pre_match_notes.append(
            "Goalserve snapshot currently uses normalized demo match data with provider-specific odds fallback."
        )
        snapshot.provider_evidence = list(
            dict.fromkeys([
                *snapshot.provider_evidence,
                f"provider={self.name}",
            ])
        )
        return snapshot

    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        fallback = self._build_demo_odds_snapshot(match_id)

        if self.settings.demo_mode or not self.settings.goalserve_api_key:
            return fallback

        try:
            payload = await self._fetch_odds_payload(match_id)
            snapshot = self._parse_odds_payload(match_id=match_id, payload=payload)
            return snapshot if snapshot.prices else fallback
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return fallback

    async def _fetch_odds_payload(self, match_id: str) -> dict[str, Any]:
        params = {
            "json": "1",
            "match_id": match_id,
            "api_key": self.settings.goalserve_api_key,
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(
                str(self.settings.goalserve_base_url).rstrip("/"),
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Unexpected Goalserve payload type")
        return payload

    def _parse_odds_payload(self, match_id: str, payload: dict[str, Any]) -> OddsSnapshot:
        prices: list[OddsPrice] = []
        last_update = self._to_optional_str(
            payload.get("last_update")
            or payload.get("updated_at")
            or payload.get("update_time")
        )

        for market in self._extract_market_nodes(payload):
            market_name = self._normalize_market_name(
                market.get("market") or market.get("type") or market.get("name") or market.get("market_name")
            )
            if market_name is None:
                continue

            bookmaker = self._to_optional_str(
                market.get("bookmaker") or market.get("book") or market.get("source")
            ) or "goalserve"
            outcomes = market.get("outcomes") or market.get("selections") or market.get("values")
            if not isinstance(outcomes, list):
                continue

            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue

                selection = self._map_selection(market_name, outcome)
                if not selection:
                    continue

                price = self._safe_float(outcome.get("price") or outcome.get("odds"), default=0.0)
                if price <= 0:
                    continue

                prices.append(
                    OddsPrice(
                        bookmaker=bookmaker,
                        market=market_name,
                        selection=selection,
                        line=self._extract_line(outcome),
                        price=price,
                    )
                )

        return OddsSnapshot(
            match_id=match_id,
            prices=prices,
            last_update=last_update,
            source=self.name,
        )

    def _extract_market_nodes(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []

        def collect(container: Any) -> None:
            if isinstance(container, dict):
                for key in ("markets", "odds"):
                    value = container.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                nodes.append(item)
                data = container.get("data")
                if isinstance(data, dict):
                    collect(data)
                elif isinstance(data, list):
                    for item in data:
                        collect(item)
            elif isinstance(container, list):
                for item in container:
                    collect(item)

        collect(payload)
        return nodes

    def _normalize_market_name(self, raw_value: Any) -> str | None:
        text = self._to_optional_str(raw_value)
        if not text:
            return None
        return MARKET_NAME_MAP.get(text.strip().lower())

    def _map_selection(self, market: str, outcome: dict[str, Any]) -> str | None:
        raw_name = self._to_optional_str(outcome.get("name") or outcome.get("selection") or outcome.get("label"))
        if not raw_name:
            return None

        normalized = raw_name.lower()
        if market == "1X2":
            if normalized in {"1", "home", "home_win", "host"}:
                return "home"
            if normalized in {"x", "draw", "tie"}:
                return "draw"
            if normalized in {"2", "away", "away_win", "guest"}:
                return "away"
        elif market == "asian_handicap":
            if normalized.startswith("home") or normalized == "1":
                return "home"
            if normalized.startswith("away") or normalized == "2":
                return "away"
        elif market == "over_under":
            if normalized.startswith("over"):
                return "over"
            if normalized.startswith("under"):
                return "under"

        return raw_name

    def _extract_line(self, outcome: dict[str, Any]) -> float | None:
        line = outcome.get("line")
        if line in (None, ""):
            line = outcome.get("handicap")
        if line in (None, ""):
            line = outcome.get("point")
        if line in (None, ""):
            return None

        try:
            return float(line)
        except (TypeError, ValueError):
            return None

    def _build_demo_odds_snapshot(self, match_id: str) -> OddsSnapshot:
        return OddsSnapshot(
            match_id=match_id,
            prices=[
                OddsPrice(bookmaker="goalserve-demo", market="1X2", selection="home", price=1.94),
                OddsPrice(bookmaker="goalserve-demo", market="1X2", selection="draw", price=3.38),
                OddsPrice(bookmaker="goalserve-demo", market="1X2", selection="away", price=4.18),
                OddsPrice(bookmaker="goalserve-demo", market="asian_handicap", selection="home", line=-0.5, price=1.96),
                OddsPrice(bookmaker="goalserve-demo", market="asian_handicap", selection="away", line=0.5, price=1.88),
                OddsPrice(bookmaker="goalserve-demo", market="over_under", selection="over", line=2.5, price=1.91),
                OddsPrice(bookmaker="goalserve-demo", market="over_under", selection="under", line=2.5, price=1.93),
            ],
            last_update="2026-01-01T10:05:00Z",
            source=self.name,
        )

    def _to_optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


__all__ = ["GoalserveProvider"]
