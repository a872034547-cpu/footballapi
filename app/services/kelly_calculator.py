"""凯利指数计算器 —— 基于多来源赔率数据自行计算凯利指数。

凯利公式: K = (p × odds - 1) / (odds - 1)
  其中 p = 1 / avg_odds (市场平均隐含概率)

对每个博彩公司分别计算，然后取平均。
凯利值 > 1 表示博彩公司高估了该结果（对庄家有利），
凯利值 < 1 表示博彩公司低估了该结果（对玩家有利）。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from app.models import BookmakerKellyDetail, KellyResult, OddsPrice, OddsSnapshot

logger = logging.getLogger(__name__)


class KellyCalculator:
    """凯利指数计算器。

    从多个 provider 的 OddsSnapshot 中提取赔率数据，
    按博彩公司分组，计算各市场的凯利指数。
    """

    def __init__(self) -> None:
        pass

    # ── 公共接口 ──────────────────────────────────────

    def calculate(self, match_id: str, odds_snapshots: list[OddsSnapshot]) -> KellyResult:
        """计算凯利指数。

        Args:
            match_id: 比赛 ID
            odds_snapshots: 来自多个 provider 的赔率快照列表

        Returns:
            KellyResult 包含各市场凯利指数和博彩公司明细
        """
        # 1. 收集所有 OddsPrice，按 bookmaker 分组
        by_bookmaker: dict[str, dict[str, list[OddsPrice]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for snapshot in odds_snapshots:
            if not snapshot or not snapshot.prices:
                continue
            for price in snapshot.prices:
                bookmaker = price.bookmaker or "unknown"
                market = price.market or ""
                by_bookmaker[bookmaker][market].append(price)

        if not by_bookmaker:
            return KellyResult(match_id=match_id, bookmaker_count=0)

        # 2. 计算市场平均赔率（用于隐含概率）
        market_avgs = self._calc_market_averages(by_bookmaker)

        # 3. 对每个博彩公司计算凯利
        details: list[BookmakerKellyDetail] = []
        for bookmaker, markets in by_bookmaker.items():
            detail = self._calc_bookmaker_kelly(bookmaker, markets, market_avgs)
            if detail:
                details.append(detail)

        # 4. 汇总平均凯利
        result = self._aggregate_kelly(match_id, details)
        return result

    # ── 内部方法 ──────────────────────────────────────

    def _calc_market_averages(
        self, by_bookmaker: dict[str, dict[str, list[OddsPrice]]]
    ) -> dict[str, dict[str, float]]:
        """计算各市场各选项的平均赔率。

        Returns:
            {"1x2": {"home": 2.10, "draw": 3.50, "away": 3.20}, ...}
        """
        # 收集所有价格
        market_prices: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for bookmaker, markets in by_bookmaker.items():
            for market, prices in markets.items():
                for p in prices:
                    selection = p.selection or ""
                    if p.price and p.price > 1.0:
                        market_prices[market][selection].append(p.price)

        # 计算平均
        avgs: dict[str, dict[str, float]] = {}
        for market, selections in market_prices.items():
            avgs[market] = {}
            for sel, prices in selections.items():
                if prices:
                    avgs[market][sel] = sum(prices) / len(prices)

        return avgs

    def _calc_bookmaker_kelly(
        self,
        bookmaker: str,
        markets: dict[str, list[OddsPrice]],
        market_avgs: dict[str, dict[str, float]],
    ) -> BookmakerKellyDetail | None:
        """计算单个博彩公司的凯利指数。"""
        detail = BookmakerKellyDetail(bookmaker=bookmaker)

        # 1x2 凯利
        if "1x2" in markets:
            avg_1x2 = market_avgs.get("1x2", {})
            for p in markets["1x2"]:
                sel = p.selection or ""
                avg_odds = avg_1x2.get(sel)
                if avg_odds and avg_odds > 1.0 and p.price and p.price > 1.0:
                    kelly = self._kelly_formula(avg_odds, p.price)
                    if sel == "home":
                        detail.home_kelly = kelly
                    elif sel == "draw":
                        detail.draw_kelly = kelly
                    elif sel == "away":
                        detail.away_kelly = kelly

        # 大小球凯利
        if "over_under" in markets:
            avg_ou = market_avgs.get("over_under", {})
            for p in markets["over_under"]:
                sel = p.selection or ""
                avg_odds = avg_ou.get(sel)
                if avg_odds and avg_odds > 1.0 and p.price and p.price > 1.0:
                    kelly = self._kelly_formula(avg_odds, p.price)
                    if sel == "over":
                        detail.over_kelly = kelly
                    elif sel == "under":
                        detail.under_kelly = kelly

        # 亚盘凯利
        if "asian_handicap" in markets:
            avg_ah = market_avgs.get("asian_handicap", {})
            for p in markets["asian_handicap"]:
                sel = p.selection or ""
                avg_odds = avg_ah.get(sel)
                if avg_odds and avg_odds > 1.0 and p.price and p.price > 1.0:
                    kelly = self._kelly_formula(avg_odds, p.price)
                    if sel == "home":
                        detail.asian_home_kelly = kelly
                    elif sel == "away":
                        detail.asian_away_kelly = kelly

        # 如果没有任何有效凯利值，跳过
        if all(
            v is None
            for v in [
                detail.home_kelly,
                detail.draw_kelly,
                detail.away_kelly,
                detail.over_kelly,
                detail.under_kelly,
                detail.asian_home_kelly,
                detail.asian_away_kelly,
            ]
        ):
            return None

        return detail

    @staticmethod
    def _kelly_formula(avg_odds: float, bookmaker_odds: float) -> float:
        """凯利公式: K = (p × odds - 1) / (odds - 1)

        p = 1 / avg_odds (市场平均隐含概率)
        """
        if bookmaker_odds <= 1.0:
            return 0.0
        p = 1.0 / avg_odds if avg_odds > 0 else 0.0
        kelly = (p * bookmaker_odds - 1.0) / (bookmaker_odds - 1.0)
        return round(kelly, 4)

    def _aggregate_kelly(
        self, match_id: str, details: list[BookmakerKellyDetail]
    ) -> KellyResult:
        """汇总所有博彩公司的凯利指数，取平均。"""
        if not details:
            return KellyResult(match_id=match_id, bookmaker_count=0)

        def _safe_avg(values: list[float]) -> float | None:
            valid = [v for v in values if v is not None]
            return round(sum(valid) / len(valid), 4) if valid else None

        home_vals = [d.home_kelly for d in details if d.home_kelly is not None]
        draw_vals = [d.draw_kelly for d in details if d.draw_kelly is not None]
        away_vals = [d.away_kelly for d in details if d.away_kelly is not None]
        over_vals = [d.over_kelly for d in details if d.over_kelly is not None]
        under_vals = [d.under_kelly for d in details if d.under_kelly is not None]
        ah_home_vals = [d.asian_home_kelly for d in details if d.asian_home_kelly is not None]
        ah_away_vals = [d.asian_away_kelly for d in details if d.asian_away_kelly is not None]

        return KellyResult(
            match_id=match_id,
            home_kelly=_safe_avg(home_vals),
            draw_kelly=_safe_avg(draw_vals),
            away_kelly=_safe_avg(away_vals),
            over_kelly=_safe_avg(over_vals),
            under_kelly=_safe_avg(under_vals),
            asian_home_kelly=_safe_avg(ah_home_vals),
            asian_away_kelly=_safe_avg(ah_away_vals),
            bookmaker_count=len(details),
            bookmaker_details=details,
            source="calculated",
        )


__all__ = ["KellyCalculator"]