from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, stdev

from app.models import (
    MatchSnapshot,
    OddsPrice,
    OddsSnapshot,
    PatternMatchResult,
    RollingPrediction,
    TeamForm,
)

# ── 亚盘让球线映射表 ──────────────────────────────────────────
# 预期进球差 → 标准亚盘让球线
_GOAL_DIFF_TO_HANDICAP: list[tuple[float, float]] = [
    (0.00, 0.00),   # 平手
    (0.15, 0.25),   # 平/半
    (0.35, 0.50),   # 半球
    (0.55, 0.75),   # 半/一
    (0.75, 1.00),   # 一球
    (0.95, 1.25),   # 一球/球半
    (1.15, 1.50),   # 球半
    (1.35, 1.75),   # 球半/两球
    (1.55, 2.00),   # 两球
    (1.80, 2.25),   # 两球/两球半
    (2.05, 2.50),   # 两球半
    (2.35, 2.75),   # 两球半/三球
    (2.65, 3.00),   # 三球
]


@dataclass(slots=True)
class _BookmakerOdds:
    """单家博彩公司的 1x2 赔率记录"""
    bookmaker: str
    home_odds: float
    draw_odds: float
    away_odds: float
    home_implied: float = 0.0
    draw_implied: float = 0.0
    away_implied: float = 0.0

    def __post_init__(self) -> None:
        total = 1.0 / self.home_odds + 1.0 / self.draw_odds + 1.0 / self.away_odds
        if total > 0:
            self.home_implied = (1.0 / self.home_odds) / total
            self.draw_implied = (1.0 / self.draw_odds) / total
            self.away_implied = (1.0 / self.away_odds) / total


@dataclass(slots=True)
class _AsianWaterRecord:
    """亚盘水位记录"""
    bookmaker: str
    line: float       # 盘口线（正=主让，负=客让）
    water: float      # 水位（赔率）
    is_opening: bool  # 是否初盘


@dataclass(slots=True)
class _TotalsInput:
    """大小球分析输入"""
    home_goals_for: float
    home_goals_against: float
    away_goals_for: float
    away_goals_against: float
    h2h_avg_goals: float | None       # 历史交锋场均总进球
    market_totals_line: float | None   # 市场大小球盘口线


# ── 辅助函数 ──────────────────────────────────────────────────


def _goal_diff_to_handicap(goal_diff: float) -> float:
    """将预期进球差映射到最近的亚盘让球线"""
    abs_diff = abs(goal_diff)
    sign = 1.0 if goal_diff >= 0 else -1.0
    best_line = 0.0
    best_dist = float("inf")
    for threshold, line in _GOAL_DIFF_TO_HANDICAP:
        dist = abs(abs_diff - threshold)
        if dist < best_dist:
            best_dist = dist
            best_line = line
    return sign * best_line


def _handicap_to_label(line: float) -> str:
    """将让球线转为可读标签"""
    if line == 0:
        return "平手"
    sign = "主让" if line > 0 else "客让"
    value = abs(line)
    if value == 0.25:
        return f"{sign}平/半"
    if value == 0.50:
        return f"{sign}半球"
    if value == 0.75:
        return f"{sign}半/一"
    if value == 1.00:
        return f"{sign}一球"
    if value == 1.25:
        return f"{sign}一球/球半"
    if value == 1.50:
        return f"{sign}球半"
    if value == 1.75:
        return f"{sign}球半/两球"
    if value == 2.00:
        return f"{sign}两球"
    return f"{sign}{value}球"


def _extract_1x2_by_bookmaker(prices: list[OddsPrice]) -> dict[str, _BookmakerOdds]:
    """从 OddsPrice 列表中按博彩公司分组提取 1x2 赔率"""
    raw: dict[str, dict[str, float]] = {}
    for price in prices:
        market = price.market.strip().lower()
        if market not in {"1x2", "match_winner", "full_time_result"}:
            continue
        selection = price.selection.strip().lower()
        key: str | None = None
        if selection in {"1", "home", "home_win", "host"}:
            key = "home"
        elif selection in {"x", "draw", "tie"}:
            key = "draw"
        elif selection in {"2", "away", "away_win", "guest"}:
            key = "away"
        if key is None or price.price <= 1.0:
            continue
        bookmaker = price.bookmaker.strip() or "unknown"
        raw.setdefault(bookmaker, {})[key] = price.price

    result: dict[str, _BookmakerOdds] = {}
    for bookmaker, odds_map in raw.items():
        if "home" in odds_map and "draw" in odds_map and "away" in odds_map:
            result[bookmaker] = _BookmakerOdds(
                bookmaker=bookmaker,
                home_odds=odds_map["home"],
                draw_odds=odds_map["draw"],
                away_odds=odds_map["away"],
            )
    return result


def _extract_asian_handicap_prices(prices: list[OddsPrice]) -> list[_AsianWaterRecord]:
    """从 OddsPrice 列表中提取亚盘数据"""
    records: list[_AsianWaterRecord] = []
    for price in prices:
        market = price.market.strip().lower()
        if "asian" not in market and "handicap" not in market:
            continue
        if price.line is None or price.price <= 1.0:
            continue
        is_opening = "open" in market or "initial" in market or "初" in market
        records.append(
            _AsianWaterRecord(
                bookmaker=price.bookmaker.strip() or "unknown",
                line=price.line,
                water=price.price,
                is_opening=is_opening,
            )
        )
    return records


# ── PatternPredictor ──────────────────────────────────────────


class PatternPredictor:
    """历史模式匹配预测器

    通过四个维度（让球推导、欧赔隐含概率、亚盘水位、大小球预期）
    分析比赛模式，综合信号一致性输出信心等级 A/B/C/D。
    """

    # 欧赔异常检测阈值（标准差倍数）
    ODDS_ANOMALY_STD_MULTIPLIER: float = 1.5
    # 水位变化显著阈值
    WATER_CHANGE_THRESHOLD: float = 0.08
    # 盘口变化显著阈值
    LINE_CHANGE_THRESHOLD: float = 0.25

    # ── 公开入口 ──────────────────────────────────────────────

    def analyze_patterns(self, snapshot: MatchSnapshot) -> list[PatternMatchResult]:
        """返回所有四个维度的模式匹配结果"""
        return [
            self.derive_handicap(snapshot),
            self.analyze_odds_implied(snapshot),
            self.analyze_asian_water(snapshot),
            self.analyze_totals(snapshot),
        ]

    def calculate_confidence_level(self, patterns: list[PatternMatchResult]) -> str:
        """综合四个维度信号一致性，输出 A/B/C/D 等级

        判断标准：每个 pattern 的 conclusion 是否包含方向性信号
        （如 "看好主队" / "倾向大球" 等），统计一致方向的数量。
        """
        signals: list[str] = []
        for p in patterns:
            direction = self._extract_signal_direction(p)
            if direction:
                signals.append(direction)

        if not signals:
            return "D"

        # 统计多数方向
        home_count = sum(1 for s in signals if s == "home")
        away_count = sum(1 for s in signals if s == "away")
        over_count = sum(1 for s in signals if s == "over")
        under_count = sum(1 for s in signals if s == "under")

        max_agree = max(home_count, away_count, over_count, under_count)

        if max_agree >= 4:
            return "A"
        if max_agree >= 3:
            return "B"
        if max_agree >= 2:
            return "C"
        return "D"

    # ── 1. 让球推导 ───────────────────────────────────────────

    def derive_handicap(self, snapshot: MatchSnapshot) -> PatternMatchResult:
        """从欧赔隐含概率反推合理让球线，与市场盘口对比"""
        bookmakers = self._get_bookmaker_odds(snapshot)
        if not bookmakers:
            return PatternMatchResult(
                pattern_type="handicap_derivation",
                description="让球推导：缺少有效 1x2 赔率，无法推导合理让球线",
                similarity=0.0,
                evidence=["无可用赔率数据"],
                derived_line=None,
                market_line=None,
                conclusion="数据不足，无法判断",
            )

        # 加权平均隐含概率（按博彩公司数量等权）
        avg_home = mean(b.home_implied for b in bookmakers.values())
        avg_draw = mean(b.draw_implied for b in bookmakers.values())
        avg_away = mean(b.away_implied for b in bookmakers.values())

        # 预期进球差：基于胜平负概率的经验映射
        # 胜率每高 10%，预期净胜球约 +0.25
        goal_diff = (avg_home - avg_away) * 2.5 + (avg_home - 0.33) * 0.5
        derived_line = _goal_diff_to_handicap(goal_diff)

        # 获取市场盘口线
        market_line = self._extract_market_handicap_line(snapshot)

        evidence: list[str] = [
            f"欧赔隐含概率：主胜={avg_home:.1%} 平={avg_draw:.1%} 客胜={avg_away:.1%}",
            f"预期进球差={goal_diff:+.2f} → 推导让球线={_handicap_to_label(derived_line)} ({derived_line:+.2f})",
        ]

        if market_line is not None:
            evidence.append(f"市场盘口线={_handicap_to_label(market_line)} ({market_line:+.2f})")
            line_diff = derived_line - market_line
            evidence.append(f"推导线与市场线差值={line_diff:+.2f}")

            if abs(line_diff) <= 0.25:
                conclusion = f"推导让球线 {_handicap_to_label(derived_line)} 与市场盘口 {_handicap_to_label(market_line)} 基本一致，盘口合理"
                similarity = 85.0
            elif line_diff > 0:
                conclusion = f"推导让球线 {_handicap_to_label(derived_line)} 深于市场盘口 {_handicap_to_label(market_line)}，市场可能低估主队实力"
                similarity = 60.0
            else:
                conclusion = f"推导让球线 {_handicap_to_label(derived_line)} 浅于市场盘口 {_handicap_to_label(market_line)}，市场可能高估主队或存在诱盘"
                similarity = 55.0
        else:
            conclusion = f"推导合理让球线为 {_handicap_to_label(derived_line)} ({derived_line:+.2f})，缺少市场盘口对比"
            similarity = 50.0

        return PatternMatchResult(
            pattern_type="handicap_derivation",
            description=f"让球推导：基于欧赔隐含概率推导合理让球线",
            similarity=round(similarity, 1),
            evidence=evidence,
            derived_line=round(derived_line, 2),
            market_line=round(market_line, 2) if market_line is not None else None,
            conclusion=conclusion,
        )

    # ── 2. 欧赔隐含概率分析 ───────────────────────────────────

    def analyze_odds_implied(self, snapshot: MatchSnapshot) -> PatternMatchResult:
        """从多家博彩公司赔率提取隐含概率，加权平均，检测异常"""
        bookmakers = self._get_bookmaker_odds(snapshot)
        if len(bookmakers) < 2:
            return PatternMatchResult(
                pattern_type="odds_implied",
                description="欧赔分析：博彩公司数量不足（需≥2家），无法进行交叉验证",
                similarity=0.0,
                evidence=[f"可用博彩公司数量={len(bookmakers)}"],
                derived_line=None,
                market_line=None,
                conclusion="数据不足，无法判断",
            )

        home_implieds = [b.home_implied for b in bookmakers.values()]
        draw_implieds = [b.draw_implied for b in bookmakers.values()]
        away_implieds = [b.away_implied for b in bookmakers.values()]

        avg_home = mean(home_implieds)
        avg_draw = mean(draw_implieds)
        avg_away = mean(away_implieds)

        std_home = stdev(home_implieds) if len(home_implieds) >= 2 else 0.0
        std_draw = stdev(draw_implieds) if len(draw_implieds) >= 2 else 0.0
        std_away = stdev(away_implieds) if len(away_implieds) >= 2 else 0.0

        evidence: list[str] = [
            f"参与分析博彩公司={len(bookmakers)} 家",
            f"主胜隐含概率：均值={avg_home:.1%} 标准差={std_home:.3f}",
            f"平局隐含概率：均值={avg_draw:.1%} 标准差={std_draw:.3f}",
            f"客胜隐含概率：均值={avg_away:.1%} 标准差={std_away:.3f}",
        ]

        # 异常检测
        anomalies: list[str] = []
        threshold = self.ODDS_ANOMALY_STD_MULTIPLIER
        for b in bookmakers.values():
            if std_home > 0 and abs(b.home_implied - avg_home) > threshold * std_home:
                anomalies.append(f"{b.bookmaker} 主胜隐含概率异常 ({b.home_implied:.1%} vs 均值 {avg_home:.1%})")
            if std_draw > 0 and abs(b.draw_implied - avg_draw) > threshold * std_draw:
                anomalies.append(f"{b.bookmaker} 平局隐含概率异常 ({b.draw_implied:.1%} vs 均值 {avg_draw:.1%})")
            if std_away > 0 and abs(b.away_implied - avg_away) > threshold * std_away:
                anomalies.append(f"{b.bookmaker} 客胜隐含概率异常 ({b.away_implied:.1%} vs 均值 {avg_away:.1%})")

        if anomalies:
            evidence.append(f"检测到 {len(anomalies)} 个异常赔率：")
            evidence.extend(f"  • {a}" for a in anomalies[:5])

        # 结论
        max_prob = max(avg_home, avg_draw, avg_away)
        if max_prob == avg_home and avg_home >= avg_away + 0.08:
            direction = "主胜"
            similarity = 75.0
        elif max_prob == avg_away and avg_away >= avg_home + 0.08:
            direction = "客胜"
            similarity = 70.0
        elif max_prob == avg_draw and avg_draw >= 0.30:
            direction = "平局倾向"
            similarity = 65.0
        else:
            direction = "胜负难分"
            similarity = 50.0

        if anomalies:
            conclusion = f"欧赔综合指向{direction}，但存在 {len(anomalies)} 个异常赔率需警惕"
            similarity = max(similarity - 10.0, 30.0)
        else:
            conclusion = f"欧赔综合指向{direction}，各博彩公司赔率一致性良好"

        return PatternMatchResult(
            pattern_type="odds_implied",
            description="欧赔隐含概率分析：多博彩公司交叉验证",
            similarity=round(similarity, 1),
            evidence=evidence,
            derived_line=None,
            market_line=None,
            conclusion=conclusion,
        )

    # ── 3. 亚盘水位分析 ───────────────────────────────────────

    def analyze_asian_water(self, snapshot: MatchSnapshot) -> PatternMatchResult:
        """分析盘口升降方向 + 水位变化，判断机构真实意图"""
        records = self._get_asian_records(snapshot)
        if not records:
            return PatternMatchResult(
                pattern_type="asian_water",
                description="亚盘水位分析：缺少亚盘数据",
                similarity=0.0,
                evidence=["无可用亚盘赔率数据"],
                derived_line=None,
                market_line=None,
                conclusion="数据不足，无法判断",
            )

        # 按博彩公司分组
        by_bookmaker: dict[str, list[_AsianWaterRecord]] = {}
        for r in records:
            by_bookmaker.setdefault(r.bookmaker, []).append(r)

        evidence: list[str] = [f"参与分析博彩公司={len(by_bookmaker)} 家"]
        signals: list[str] = []

        for bookmaker, recs in by_bookmaker.items():
            openings = [r for r in recs if r.is_opening]
            currents = [r for r in recs if not r.is_opening]

            if not openings or not currents:
                continue

            avg_open_line = mean(r.line for r in openings)
            avg_open_water = mean(r.water for r in openings)
            avg_cur_line = mean(r.line for r in currents)
            avg_cur_water = mean(r.water for r in currents)

            line_change = avg_cur_line - avg_open_line
            water_change = avg_cur_water - avg_open_water

            evidence.append(
                f"{bookmaker}: 初盘={avg_open_line:+.2f}(水{avg_open_water:.2f}) → "
                f"即时={avg_cur_line:+.2f}(水{avg_cur_water:.2f}) "
                f"盘口变化={line_change:+.2f} 水位变化={water_change:+.2f}"
            )

            # 信号判断
            if abs(line_change) >= self.LINE_CHANGE_THRESHOLD or abs(water_change) >= self.WATER_CHANGE_THRESHOLD:
                if line_change > 0 and water_change < 0:
                    signals.append("升盘降水→看好主队")
                elif line_change < 0 and water_change > 0:
                    signals.append("降盘升水→看衰主队")
                elif line_change > 0 and water_change > 0:
                    signals.append("升盘升水→诱盘嫌疑")
                elif line_change < 0 and water_change < 0:
                    signals.append("降盘降水→谨慎看好客队")
                elif water_change < -self.WATER_CHANGE_THRESHOLD:
                    signals.append("降水→机构防范赔付")
                elif water_change > self.WATER_CHANGE_THRESHOLD:
                    signals.append("升水→机构不惧赔付")

        if not signals:
            conclusion = "亚盘水位变化不显著，机构态度中性"
            similarity = 45.0
        else:
            # 统计信号方向
            bullish = sum(1 for s in signals if "看好主队" in s or "防范赔付" in s)
            bearish = sum(1 for s in signals if "看衰主队" in s or "诱盘" in s or "看好客队" in s)
            evidence.append(f"信号汇总：看好主队={bullish} 看衰主队={bearish}")

            if bullish > bearish:
                conclusion = f"亚盘水位综合偏向看好主队（{bullish}/{len(signals)} 信号）"
                similarity = 55.0 + bullish * 8.0
            elif bearish > bullish:
                conclusion = f"亚盘水位综合偏向看衰主队（{bearish}/{len(signals)} 信号）"
                similarity = 55.0 + bearish * 8.0
            else:
                conclusion = "亚盘水位信号分歧，方向不明确"
                similarity = 48.0

        return PatternMatchResult(
            pattern_type="asian_water",
            description="亚盘水位分析：盘口升降与水位变化",
            similarity=round(min(similarity, 90.0), 1),
            evidence=evidence,
            derived_line=None,
            market_line=None,
            conclusion=conclusion,
        )

    # ── 4. 大小球预期 ─────────────────────────────────────────

    def analyze_totals(self, snapshot: MatchSnapshot) -> PatternMatchResult:
        """基于近期攻防数据 + 历史交锋推导预期总进球"""
        home_form = snapshot.home_form
        away_form = snapshot.away_form

        totals_input = _TotalsInput(
            home_goals_for=home_form.goals_for_avg,
            home_goals_against=home_form.goals_against_avg,
            away_goals_for=away_form.goals_for_avg,
            away_goals_against=away_form.goals_against_avg,
            h2h_avg_goals=None,  # 当前 snapshot 无 h2h 字段，后续可扩展
            market_totals_line=self._extract_market_totals_line(snapshot),
        )

        # 预期总进球 = 主队预期进球 + 客队预期进球
        home_expected = (
            totals_input.home_goals_for * 0.55
            + totals_input.away_goals_against * 0.45
        )
        away_expected = (
            totals_input.away_goals_for * 0.55
            + totals_input.home_goals_against * 0.45
        )

        # 兜底
        if home_expected <= 0:
            home_expected = 1.2
        if away_expected <= 0:
            away_expected = 1.0

        # 历史交锋修正
        if totals_input.h2h_avg_goals is not None:
            h2h_weight = 0.20
            raw_total = home_expected + away_expected
            blended_total = raw_total * (1 - h2h_weight) + totals_input.h2h_avg_goals * h2h_weight
            home_expected = home_expected * (blended_total / raw_total) if raw_total > 0 else home_expected
            away_expected = away_expected * (blended_total / raw_total) if raw_total > 0 else away_expected

        total_expected = home_expected + away_expected

        evidence: list[str] = [
            f"主队场均进球={totals_input.home_goals_for:.2f} 失球={totals_input.home_goals_against:.2f}",
            f"客队场均进球={totals_input.away_goals_for:.2f} 失球={totals_input.away_goals_against:.2f}",
            f"预期进球：主={home_expected:.2f} 客={away_expected:.2f} 总={total_expected:.2f}",
        ]

        if totals_input.h2h_avg_goals is not None:
            evidence.append(f"历史交锋场均总进球={totals_input.h2h_avg_goals:.2f}（已加权融合）")

        market_line = totals_input.market_totals_line or 2.5
        evidence.append(f"市场大小球盘口线={market_line}")

        diff = total_expected - market_line

        if diff >= 0.4:
            conclusion = f"预期总进球 {total_expected:.2f} 明显高于盘口 {market_line}，倾向大球"
            similarity = 65.0 + min(diff * 10, 20)
        elif diff >= 0.15:
            conclusion = f"预期总进球 {total_expected:.2f} 略高于盘口 {market_line}，轻微倾向大球"
            similarity = 58.0
        elif diff >= -0.15:
            conclusion = f"预期总进球 {total_expected:.2f} 与盘口 {market_line} 接近，大小球方向不明确"
            similarity = 50.0
        elif diff >= -0.4:
            conclusion = f"预期总进球 {total_expected:.2f} 略低于盘口 {market_line}，轻微倾向小球"
            similarity = 58.0
        else:
            conclusion = f"预期总进球 {total_expected:.2f} 明显低于盘口 {market_line}，倾向小球"
            similarity = 65.0 + min(abs(diff) * 10, 20)

        return PatternMatchResult(
            pattern_type="totals",
            description="大小球预期：基于攻防数据推导预期总进球",
            similarity=round(min(similarity, 88.0), 1),
            evidence=evidence,
            derived_line=round(total_expected, 2),
            market_line=market_line,
            conclusion=conclusion,
        )

    # ── 5. 滚球预测 ───────────────────────────────────────────

    def build_rolling_prediction(
        self,
        snapshot: MatchSnapshot,
        current_minute: int,
        home_score: int,
        away_score: int,
    ) -> list[RollingPrediction]:
        """基于当前比分和时间生成滚球预测"""
        match_id = snapshot.match.id
        current_score = f"{home_score}-{away_score}"
        remaining_minutes = max(90 - current_minute, 1)
        remaining_ratio = remaining_minutes / 90.0

        # 获取赛前基础概率
        bookmakers = self._get_bookmaker_odds(snapshot)
        if bookmakers:
            base_home = mean(b.home_implied for b in bookmakers.values())
            base_draw = mean(b.draw_implied for b in bookmakers.values())
            base_away = mean(b.away_implied for b in bookmakers.values())
        else:
            # 从 form 估算
            home_ppm = snapshot.home_form.points_per_match
            away_ppm = snapshot.away_form.points_per_match
            total_ppm = home_ppm + away_ppm or 1
            base_home = home_ppm / total_ppm * 0.7
            base_draw = 0.26
            base_away = away_ppm / total_ppm * 0.7
            s = base_home + base_draw + base_away
            if s > 0:
                base_home /= s
                base_draw /= s
                base_away /= s

        predictions: list[RollingPrediction] = []

        # ── 剩余时间胜平负 ──
        goal_diff = home_score - away_score
        wdl_pred = self._adjust_wdl_for_live(
            base_home, base_draw, base_away,
            goal_diff, remaining_ratio, current_minute,
        )
        for label, prob, rationale in wdl_pred:
            predictions.append(
                RollingPrediction(
                    match_id=match_id,
                    minute=current_minute,
                    current_score=current_score,
                    prediction_type="win_draw_lose",
                    label=label,
                    probability=round(prob * 100, 2),
                    rationale=rationale,
                    confidence_level=self._live_confidence(remaining_ratio, abs(goal_diff)),
                )
            )

        # ── 大小球调整 ──
        totals_pattern = self.analyze_totals(snapshot)
        base_total = totals_pattern.derived_line or 2.5
        # 已进球数
        scored = home_score + away_score
        # 剩余时间预期进球
        remaining_expected = base_total * remaining_ratio * 0.85  # 体能下降因子
        projected_total = scored + remaining_expected

        market_line = totals_pattern.market_line or 2.5
        if projected_total >= market_line + 0.3:
            ou_label = "over"
            ou_prob = 0.55 + min((projected_total - market_line) * 0.12, 0.25)
            ou_rationale = f"已进{scored}球，剩余时间预期{remaining_expected:.1f}球，总进球预计{projected_total:.1f}，倾向大球"
        elif projected_total <= market_line - 0.3:
            ou_label = "under"
            ou_prob = 0.55 + min((market_line - projected_total) * 0.12, 0.25)
            ou_rationale = f"已进{scored}球，剩余时间预期{remaining_expected:.1f}球，总进球预计{projected_total:.1f}，倾向小球"
        else:
            ou_label = "over" if projected_total >= market_line else "under"
            ou_prob = 0.50
            ou_rationale = f"已进{scored}球，剩余时间预期{remaining_expected:.1f}球，总进球预计{projected_total:.1f}，方向不明确"

        predictions.append(
            RollingPrediction(
                match_id=match_id,
                minute=current_minute,
                current_score=current_score,
                prediction_type="over_under",
                label=ou_label,
                probability=round(ou_prob * 100, 2),
                rationale=ou_rationale,
                confidence_level=self._live_confidence(remaining_ratio, abs(goal_diff)),
            )
        )

        # ── 下一进球方预测 ──
        next_goal = self._predict_next_goal(
            base_home, base_away, goal_diff, remaining_ratio, current_minute,
        )
        predictions.append(
            RollingPrediction(
                match_id=match_id,
                minute=current_minute,
                current_score=current_score,
                prediction_type="next_goal",
                label=next_goal[0],
                probability=round(next_goal[1] * 100, 2),
                rationale=next_goal[2],
                confidence_level="D",  # 下一进球方预测天然低置信
            )
        )

        return predictions

    # ── 内部辅助方法 ──────────────────────────────────────────

    def _get_bookmaker_odds(self, snapshot: MatchSnapshot) -> dict[str, _BookmakerOdds]:
        """安全提取博彩公司赔率"""
        if not snapshot.odds or not snapshot.odds.prices:
            return {}
        return _extract_1x2_by_bookmaker(snapshot.odds.prices)

    def _get_asian_records(self, snapshot: MatchSnapshot) -> list[_AsianWaterRecord]:
        """安全提取亚盘记录"""
        if not snapshot.odds or not snapshot.odds.prices:
            return []
        return _extract_asian_handicap_prices(snapshot.odds.prices)

    def _extract_market_handicap_line(self, snapshot: MatchSnapshot) -> float | None:
        """从赔率数据中提取市场亚盘盘口线"""
        records = self._get_asian_records(snapshot)
        currents = [r for r in records if not r.is_opening]
        if currents:
            return mean(r.line for r in currents)
        if records:
            return mean(r.line for r in records)
        return None

    def _extract_market_totals_line(self, snapshot: MatchSnapshot) -> float | None:
        """从赔率数据中提取市场大小球盘口线"""
        if not snapshot.odds or not snapshot.odds.prices:
            return None
        totals_lines: list[float] = []
        for price in snapshot.odds.prices:
            market = price.market.strip().lower()
            if "over" in market or "under" in market or "totals" in market or "总" in market:
                if price.line is not None:
                    totals_lines.append(price.line)
        return mean(totals_lines) if totals_lines else None

    def _extract_signal_direction(self, pattern: PatternMatchResult) -> str | None:
        """从 PatternMatchResult 的 conclusion 中提取方向信号"""
        c = pattern.conclusion.lower()
        if pattern.pattern_type == "totals":
            if "大球" in c:
                return "over"
            if "小球" in c:
                return "under"
            return None
        # 胜平负方向
        if any(w in c for w in ("看好主队", "主胜", "低估主队")):
            return "home"
        if any(w in c for w in ("看好客队", "客胜", "看衰主队")):
            return "away"
        return None

    def _adjust_wdl_for_live(
        self,
        base_home: float,
        base_draw: float,
        base_away: float,
        goal_diff: int,
        remaining_ratio: float,
        minute: int,
    ) -> list[tuple[str, float, str]]:
        """根据当前比分调整剩余时间胜平负概率"""
        # 领先方心理因素：领先越大越保守，落后方越激进
        if goal_diff > 0:
            # 主队领先
            lead_factor = min(goal_diff * 0.12, 0.35)
            adj_home = base_home + lead_factor * remaining_ratio
            adj_away = base_away - lead_factor * 0.7 * remaining_ratio
            adj_draw = base_draw + lead_factor * 0.15 * remaining_ratio
        elif goal_diff < 0:
            # 客队领先
            lead_factor = min(abs(goal_diff) * 0.12, 0.35)
            adj_home = base_home - lead_factor * 0.7 * remaining_ratio
            adj_away = base_away + lead_factor * remaining_ratio
            adj_draw = base_draw + lead_factor * 0.15 * remaining_ratio
        else:
            adj_home = base_home
            adj_away = base_away
            adj_draw = base_draw

        # 时间越少，平局概率上升（如果分差小）
        if abs(goal_diff) <= 1 and remaining_ratio < 0.3:
            adj_draw += 0.08 * (1 - remaining_ratio)

        # 归一化
        total = adj_home + adj_draw + adj_away
        if total > 0:
            adj_home /= total
            adj_draw /= total
            adj_away /= total

        results: list[tuple[str, float, str]] = []
        if adj_home >= adj_away:
            results.append(("home_win", adj_home, f"当前比分下主队剩余时间胜率 {adj_home:.1%}"))
            results.append(("draw", adj_draw, f"平局概率 {adj_draw:.1%}"))
            results.append(("away_win", adj_away, f"客队逆转概率 {adj_away:.1%}"))
        else:
            results.append(("away_win", adj_away, f"当前比分下客队剩余时间胜率 {adj_away:.1%}"))
            results.append(("draw", adj_draw, f"平局概率 {adj_draw:.1%}"))
            results.append(("home_win", adj_home, f"主队逆转概率 {adj_home:.1%}"))

        return results

    def _predict_next_goal(
        self,
        base_home: float,
        base_away: float,
        goal_diff: int,
        remaining_ratio: float,
        minute: int,
    ) -> tuple[str, float, str]:
        """预测下一进球方"""
        # 基础进球概率比
        home_attack = base_home / (base_home + base_away) if (base_home + base_away) > 0 else 0.5

        # 落后方更激进 → 进攻权重提升
        if goal_diff < 0:
            # 主队落后
            home_attack += 0.08 * min(abs(goal_diff), 3)
        elif goal_diff > 0:
            # 客队落后
            home_attack -= 0.08 * min(abs(goal_diff), 3)

        # 比赛末段，落后方更拼命
        if remaining_ratio < 0.25:
            if goal_diff < 0:
                home_attack += 0.05
            elif goal_diff > 0:
                home_attack -= 0.05

        home_attack = max(min(home_attack, 0.75), 0.25)
        away_attack = 1.0 - home_attack

        no_goal_prob = 0.35 * remaining_ratio + 0.10  # 时间越少越可能无进球

        if home_attack >= away_attack:
            return (
                "home",
                home_attack * (1 - no_goal_prob),
                f"主队下一进球概率 {home_attack:.1%}，无进球概率 {no_goal_prob:.1%}",
            )
        else:
            return (
                "away",
                away_attack * (1 - no_goal_prob),
                f"客队下一进球概率 {away_attack:.1%}，无进球概率 {no_goal_prob:.1%}",
            )

    def _live_confidence(self, remaining_ratio: float, goal_diff_abs: int) -> str:
        """滚球预测信心等级（天然低于赛前）"""
        # 剩余时间越少 + 分差越大 → 信心越高
        score = (1 - remaining_ratio) * 60 + min(goal_diff_abs, 3) * 10
        if score >= 65:
            return "B"
        if score >= 40:
            return "C"
        return "D"