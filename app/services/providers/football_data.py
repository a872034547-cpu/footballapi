from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.models import MatchRef, MatchSnapshot, TeamRef
from app.services.providers.base import BaseProvider


class FootballDataProvider(BaseProvider):
    """football-data.org 基础比赛数据提供方。"""

    name = "football_data"

    def __init__(self, settings: Any | None = None) -> None:
        super().__init__(settings or get_settings())

    def is_configured(self) -> bool:
        return bool(self.settings.demo_mode or self.settings.football_data_api_key)

    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        if self.settings.demo_mode or not self.settings.football_data_api_key:
            return []

        try:
            payload = await self._fetch_matches(date)
            return self._parse_matches(payload)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return []

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        fallback = self.build_demo_snapshot(match_id)

        if self.settings.demo_mode or not self.settings.football_data_api_key:
            fallback.pre_match_notes.append("football-data provider is running in demo mode or without API key.")
            fallback.provider_evidence.append("football_data:demo_mode_or_missing_key")
            return fallback

        try:
            payload = await self._fetch_match(match_id)
            mapped_match = self._parse_single_match(payload)
            if mapped_match is None:
                fallback.pre_match_notes.append("football-data match payload could not be normalized; demo snapshot kept.")
                fallback.provider_evidence.append("football_data:match_parse_failed")
                return fallback

            fallback.match = mapped_match
            fallback.pre_match_notes.append("Match identity hydrated from football-data.org.")
            fallback.provider_evidence.append(f"football_data:match_loaded:{mapped_match.id}")
            return fallback
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            fallback.pre_match_notes.append("football-data request failed; demo snapshot kept.")
            fallback.provider_evidence.append("football_data:request_failed")
            return fallback

    async def _fetch_matches(self, date: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if date:
            params["dateFrom"] = date
            params["dateTo"] = date

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(
                f"{str(self.settings.football_data_base_url).rstrip('/')}/matches",
                headers=self._headers,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Unexpected football-data payload type")
        return payload

    async def _fetch_match(self, match_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(
                f"{str(self.settings.football_data_base_url).rstrip('/')}/matches/{match_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Unexpected football-data match payload type")
        return payload

    def _parse_matches(self, payload: dict[str, Any]) -> list[MatchRef]:
        raw_matches = payload.get("matches")
        if not isinstance(raw_matches, list):
            return []

        matches: list[MatchRef] = []
        for item in raw_matches:
            if not isinstance(item, dict):
                continue
            mapped = self._map_match(item)
            if mapped is not None:
                matches.append(mapped)
        return matches

    def _parse_single_match(self, payload: dict[str, Any]) -> MatchRef | None:
        if isinstance(payload.get("match"), dict):
            return self._map_match(payload["match"])
        return self._map_match(payload)

    def _map_match(self, item: dict[str, Any]) -> MatchRef | None:
        match_id = item.get("id")
        if match_id is None:
            return None

        competition = item.get("competition") if isinstance(item.get("competition"), dict) else {}
        home_team = item.get("homeTeam") if isinstance(item.get("homeTeam"), dict) else {}
        away_team = item.get("awayTeam") if isinstance(item.get("awayTeam"), dict) else {}

        return MatchRef(
            id=str(match_id),
            league=str(competition.get("name") or "Unknown League"),
            kickoff=str(item.get("utcDate") or ""),
            home_team=TeamRef(
                id=str(home_team.get("id") or "home"),
                name=str(home_team.get("name") or "Home Team"),
                short_name=self._to_optional_str(home_team.get("shortName")),
            ),
            away_team=TeamRef(
                id=str(away_team.get("id") or "away"),
                name=str(away_team.get("name") or "Away Team"),
                short_name=self._to_optional_str(away_team.get("shortName")),
            ),
            status=str(item.get("status") or "SCHEDULED"),
            venue=self._to_optional_str(item.get("venue")),
        )

    def _build_demo_match_id(self, date: str | None) -> str:
        suffix = (date or "upcoming").replace(":", "-").replace("/", "-")
        return f"football-data-{suffix}-001"

    def _to_optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": str(self.settings.football_data_api_key),
            "Accept": "application/json",
        }


__all__ = ["FootballDataProvider"]
