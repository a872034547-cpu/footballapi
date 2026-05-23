from __future__ import annotations

from dataclasses import dataclass
from math import exp, factorial

from app.models import (
    AnalysisResponse,
    MarketPrediction,
    MatchSnapshot,
    OddsPrice,
    ScorePrediction,
)


@dataclass(slots=True)
class TeamMetrics:
    name: str
    form_score: float
    points_per_match: float
    goals_for_avg: float
    goals_against_avg: float
    injury_count: int
    win_rate: float
    draw_rate: float
    loss_rate: float


@dataclass(slots=True)
class ProbabilitySet:
    home: float
    draw: float
    away: float


class RuleBasedPredictor:
    """Explainable rule-based predictor for match snapshots."""

    def analyze_snapshot(self, snapshot: MatchSnapshot) -> AnalysisResponse:
        home_metrics = self._build_team_metrics(snapshot, is_home=True)
        away_metrics = self._build_team_metrics(snapshot, is_home=False)
        odds_probability = self._extract_1x2_probabilities(snapshot)

        form_probability = self._calculate_form_probabilities(home_metrics, away_metrics)
        final_probability = self._blend_probabilities(form_probability, odds_probability)

        home_expected_goals, away_expected_goals = self._estimate_expected_goals(
            home_metrics,
            away_metrics,
            final_probability,
        )

        predicted_scores = self._build_score_predictions(
            home_expected_goals,
            away_expected_goals,
        )
        over_under = self._build_over_under_predictions(
            home_expected_goals,
            away_expected_goals,
        )
        asian_handicap = self._build_asian_handicap_predictions(
            final_probability,
            home_expected_goals,
            away_expected_goals,
        )

        benefit_factors = self._build_benefit_factors(
            snapshot,
            home_metrics,
            away_metrics,
            odds_probability,
            home_expected_goals,
            away_expected_goals,
        )
        risk_factors = self._build_risk_factors(
            snapshot,
            home_metrics,
            away_metrics,
            odds_probability,
            home_expected_goals,
            away_expected_goals,
        )
        pre_match_notes = self._build_pre_match_notes(
            snapshot,
            odds_probability,
            home_expected_goals,
            away_expected_goals,
        )
        provider_evidence = self._build_provider_evidence(snapshot, odds_probability)
        confidence_score = self._calculate_confidence_score(
            snapshot,
            odds_probability,
            final_probability,
            home_metrics,
            away_metrics,
        )

        return AnalysisResponse(
            match=snapshot.match,
            confidence_score=confidence_score,
            benefit_factors=benefit_factors,
            risk_factors=risk_factors,
            pre_match_notes=pre_match_notes,
            predicted_scores=predicted_scores,
            win_draw_lose=self._build_win_draw_lose_predictions(final_probability),
            over_under=over_under,
            asian_handicap=asian_handicap,
            provider_evidence=provider_evidence,
        )

    def _build_team_metrics(self, snapshot: MatchSnapshot, *, is_home: bool) -> TeamMetrics:
        form = snapshot.home_form if is_home else snapshot.away_form
        team_name = snapshot.match.home_team.name if is_home else snapshot.match.away_team.name
        team_key = "home" if is_home else "away"

        normalized_results = [self._normalize_result(result) for result in form.last_5[:5] if result]
        wins = sum(1 for result in normalized_results if result == "W")
        draws = sum(1 for result in normalized_results if result == "D")
        losses = sum(1 for result in normalized_results if result == "L")
        sample_size = max(len(normalized_results), 1)

        injury_count = sum(
            1
            for injury in snapshot.injuries
            if injury.team.strip().lower() == team_key
            or injury.team.strip().lower() == team_name.strip().lower()
        )

        form_score = (
            (wins * 3 + draws) / (sample_size * 3) * 100
            if normalized_results
            else max(min(form.points_per_match / 3 * 100, 100), 0)
        )

        return TeamMetrics(
            name=team_name,
            form_score=form_score,
            points_per_match=form.points_per_match,
            goals_for_avg=form.goals_for_avg,
            goals_against_avg=form.goals_against_avg,
            injury_count=injury_count,
            win_rate=wins / sample_size,
            draw_rate=draws / sample_size,
            loss_rate=losses / sample_size,
        )

    def _normalize_result(self, value: str) -> str:
        result = value.strip().upper()
        if result.startswith("W"):
            return "W"
        if result.startswith("D"):
            return "D"
        if result.startswith("L"):
            return "L"
        return result[:1] or "D"

    def _extract_1x2_probabilities(self, snapshot: MatchSnapshot) -> ProbabilitySet | None:
        if not snapshot.odds or not snapshot.odds.prices:
            return None

        selections: dict[str, list[float]] = {"home": [], "draw": [], "away": []}
        for price in snapshot.odds.prices:
            market = price.market.strip().lower()
            if market not in {"1x2", "match_winner", "full_time_result"}:
                continue
            selection = self._normalize_selection(price)
            if selection and price.price > 1:
                selections[selection].append(1 / price.price)

        if not selections["home"] or not selections["draw"] or not selections["away"]:
            return None

        home_raw = sum(selections["home"]) / len(selections["home"])
        draw_raw = sum(selections["draw"]) / len(selections["draw"])
        away_raw = sum(selections["away"]) / len(selections["away"])
        total = home_raw + draw_raw + away_raw
        if total <= 0:
            return None

        return ProbabilitySet(
            home=home_raw / total,
            draw=draw_raw / total,
            away=away_raw / total,
        )

    def _normalize_selection(self, price: OddsPrice) -> str | None:
        selection = price.selection.strip().lower()
        if selection in {"1", "home", "home_win", "host"}:
            return "home"
        if selection in {"x", "draw", "tie"}:
            return "draw"
        if selection in {"2", "away", "away_win", "guest"}:
            return "away"
        return None

    def _calculate_form_probabilities(
        self,
        home: TeamMetrics,
        away: TeamMetrics,
    ) -> ProbabilitySet:
        form_edge = (home.form_score - away.form_score) / 100
        points_edge = (home.points_per_match - away.points_per_match) / 3
        scoring_edge = (home.goals_for_avg - away.goals_for_avg) / 3
        defensive_edge = (away.goals_against_avg - home.goals_against_avg) / 3
        injury_edge = (away.injury_count - home.injury_count) / 5

        weighted_edge = (
            form_edge * 0.35
            + points_edge * 0.20
            + scoring_edge * 0.20
            + defensive_edge * 0.15
            + injury_edge * 0.10
        )
        weighted_edge = max(min(weighted_edge, 0.55), -0.55)

        draw_base = 0.28 + ((home.draw_rate + away.draw_rate) / 2) * 0.10
        draw_penalty = min(abs(weighted_edge) * 0.18, 0.10)
        draw_probability = max(min(draw_base - draw_penalty, 0.34), 0.18)

        remainder = 1 - draw_probability
        home_probability = remainder * (0.5 + weighted_edge)
        away_probability = remainder - home_probability

        return self._normalize_probabilities(
            ProbabilitySet(
                home=home_probability,
                draw=draw_probability,
                away=away_probability,
            )
        )

    def _blend_probabilities(
        self,
        form_probability: ProbabilitySet,
        odds_probability: ProbabilitySet | None,
    ) -> ProbabilitySet:
        if odds_probability is None:
            return form_probability

        return self._normalize_probabilities(
            ProbabilitySet(
                home=form_probability.home * 0.55 + odds_probability.home * 0.45,
                draw=form_probability.draw * 0.55 + odds_probability.draw * 0.45,
                away=form_probability.away * 0.55 + odds_probability.away * 0.45,
            )
        )

    def _normalize_probabilities(self, probability: ProbabilitySet) -> ProbabilitySet:
        home = max(probability.home, 0.05)
        draw = max(probability.draw, 0.05)
        away = max(probability.away, 0.05)
        total = home + draw + away
        return ProbabilitySet(
            home=home / total,
            draw=draw / total,
            away=away / total,
        )

    def _estimate_expected_goals(
        self,
        home: TeamMetrics,
        away: TeamMetrics,
        probability: ProbabilitySet,
    ) -> tuple[float, float]:
        home_base = home.goals_for_avg * 0.60 + away.goals_against_avg * 0.40
        away_base = away.goals_for_avg * 0.60 + home.goals_against_avg * 0.40

        if home_base <= 0:
            home_base = 1.0 + home.points_per_match * 0.25
        if away_base <= 0:
            away_base = 0.9 + away.points_per_match * 0.25

        probability_edge = probability.home - probability.away
        home_expected_goals = home_base + probability_edge * 0.65 - home.injury_count * 0.08
        away_expected_goals = away_base - probability_edge * 0.55 - away.injury_count * 0.08

        return (
            max(min(home_expected_goals, 3.2), 0.2),
            max(min(away_expected_goals, 3.0), 0.2),
        )

    def _build_score_predictions(
        self,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[ScorePrediction]:
        score_candidates: list[ScorePrediction] = []
        for home_goals in range(0, 5):
            for away_goals in range(0, 5):
                probability = self._poisson(home_expected_goals, home_goals) * self._poisson(
                    away_expected_goals,
                    away_goals,
                )
                score_candidates.append(
                    ScorePrediction(
                        score=f"{home_goals}-{away_goals}",
                        probability=round(probability * 100, 2),
                    )
                )

        score_candidates.sort(key=lambda item: item.probability, reverse=True)
        top_scores = score_candidates[:3]
        total_probability = sum(item.probability for item in top_scores) or 1

        return [
            ScorePrediction(
                score=item.score,
                probability=round(item.probability / total_probability * 100, 2),
            )
            for item in top_scores
        ]

    def _poisson(self, expected_goals: float, actual_goals: int) -> float:
        return exp(-expected_goals) * (expected_goals**actual_goals) / factorial(actual_goals)

    def _build_win_draw_lose_predictions(self, probability: ProbabilitySet) -> list[MarketPrediction]:
        return [
            MarketPrediction(
                label="home_win",
                probability=round(probability.home * 100, 2),
                rationale="主队近期状态、攻防均值与伤停因素综合后占优。",
            ),
            MarketPrediction(
                label="draw",
                probability=round(probability.draw * 100, 2),
                rationale="平局概率由双方近况接近程度与历史平局倾向推导。",
            ),
            MarketPrediction(
                label="away_win",
                probability=round(probability.away * 100, 2),
                rationale="客队在综合评分中的反制能力与赔率修正后得到对应胜率。",
            ),
        ]

    def _build_over_under_predictions(
        self,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[MarketPrediction]:
        total_goals = home_expected_goals + away_expected_goals
        over_probability = max(min(0.50 + (total_goals - 2.5) * 0.18, 0.78), 0.22)
        under_probability = 1 - over_probability

        if total_goals >= 3.0:
            over_rationale = "双方场均总进球预期偏高，倾向大于 2.5 球。"
            under_rationale = "若临场节奏下降，2-1/1-1 仍可能压低总进球。"
        else:
            over_rationale = "只有在比赛开放度提升时，才更容易打出大球。"
            under_rationale = "双方进攻期望值较克制，更偏向小于 2.5 球。"

        return [
            MarketPrediction(
                label="over_2.5",
                probability=round(over_probability * 100, 2),
                rationale=over_rationale,
            ),
            MarketPrediction(
                label="under_2.5",
                probability=round(under_probability * 100, 2),
                rationale=under_rationale,
            ),
        ]

    def _build_asian_handicap_predictions(
        self,
        probability: ProbabilitySet,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[MarketPrediction]:
        goal_edge = home_expected_goals - away_expected_goals
        if probability.home >= 0.50 and goal_edge >= 0.45:
            line = "home_-0.5"
            rationale = "主队胜率与净胜球期望同步占优，适合让 0.5 球思路。"
            confidence = max(min(probability.home + goal_edge * 0.12, 0.82), 0.40)
        elif probability.away >= 0.42 and goal_edge <= -0.20:
            line = "away_0.0"
            rationale = "客队不败面较强，且净胜球预期未落下风。"
            confidence = max(min(probability.away + abs(goal_edge) * 0.10, 0.76), 0.38)
        else:
            line = "home_0.0"
            rationale = "双方强弱差有限，更适合主队平手或走水保护。"
            confidence = max(min(probability.home + max(goal_edge, 0) * 0.08, 0.70), 0.36)

        opposite_confidence = max(1 - confidence, 0.20)
        opposite_label = "away_+0.5" if line == "home_-0.5" else "away_0.0"
        opposite_rationale = "若比赛节奏保守或主队兑现不足，对冲方向具备一定保护性。"

        return [
            MarketPrediction(
                label=line,
                probability=round(confidence * 100, 2),
                rationale=rationale,
            ),
            MarketPrediction(
                label=opposite_label,
                probability=round(opposite_confidence * 100, 2),
                rationale=opposite_rationale,
            ),
        ]

    def _build_benefit_factors(
        self,
        snapshot: MatchSnapshot,
        home: TeamMetrics,
        away: TeamMetrics,
        odds_probability: ProbabilitySet | None,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[str]:
        benefit_factors: list[str] = []

        if home.form_score >= away.form_score + 10:
            benefit_factors.append(
                f"主队近 5 场状态更好，form 评分 {home.form_score:.1f} 对 {away.form_score:.1f}。"
            )
        if home.goals_for_avg >= away.goals_for_avg + 0.3:
            benefit_factors.append(
                f"主队场均进球 {home.goals_for_avg:.2f}，高于客队 {away.goals_for_avg:.2f}。"
            )
        if away.goals_against_avg >= home.goals_against_avg + 0.3:
            benefit_factors.append(
                f"客队防守失球偏多，场均失球 {away.goals_against_avg:.2f}。"
            )
        if home.injury_count < away.injury_count:
            benefit_factors.append(
                f"主队伤停更少（{home.injury_count} vs {away.injury_count}），阵容完整度更高。"
            )
        if odds_probability and odds_probability.home > odds_probability.away:
            benefit_factors.append(
                f"赔率隐含概率支持主队不败，主胜隐含概率 {odds_probability.home * 100:.1f}%。"
            )
        if home_expected_goals >= away_expected_goals + 0.35:
            benefit_factors.append(
                f"模型期望进球主队 {home_expected_goals:.2f} 比 {away_expected_goals:.2f} 更高。"
            )

        if not benefit_factors:
            benefit_factors.append("双方基础面差距有限，暂无单边强利好，需关注临场数据。")

        return benefit_factors

    def _build_risk_factors(
        self,
        snapshot: MatchSnapshot,
        home: TeamMetrics,
        away: TeamMetrics,
        odds_probability: ProbabilitySet | None,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[str]:
        risk_factors: list[str] = []

        if abs(home.form_score - away.form_score) <= 8:
            risk_factors.append("双方近 5 场状态差距不大，赛果波动空间较高。")
        if abs(home_expected_goals - away_expected_goals) <= 0.25:
            risk_factors.append("两队期望进球接近，比分容易落入一球差或平局。")
        if home.injury_count >= 2 or away.injury_count >= 2:
            risk_factors.append("至少一方存在较多伤停，实际首发变化可能放大预测误差。")
        if not odds_probability:
            risk_factors.append("当前缺少有效赔率，仅依赖 form 与攻防均值做退化预测，置信度会下降。")
        elif abs(odds_probability.home - odds_probability.away) <= 0.08:
            risk_factors.append("赔率隐含概率接近，市场对胜负方向并未形成强共识。")
        if snapshot.weather:
            risk_factors.append(f"天气因素为“{snapshot.weather}”，可能影响节奏与总进球兑现。")

        if not risk_factors:
            risk_factors.append("主要风险集中在临场战术与盘口波动，赛前需复核首发名单。")

        return risk_factors

    def _build_pre_match_notes(
        self,
        snapshot: MatchSnapshot,
        odds_probability: ProbabilitySet | None,
        home_expected_goals: float,
        away_expected_goals: float,
    ) -> list[str]:
        notes = list(snapshot.pre_match_notes)

        notes.append(
            f"规则模型综合近况、场均进失球、伤停与{'赔率' if odds_probability else '基础面'}生成预测。"
        )
        notes.append(
            f"期望进球估计为主队 {home_expected_goals:.2f}、客队 {away_expected_goals:.2f}。"
        )
        if odds_probability:
            notes.append(
                "赔率已转为隐含概率并做去水处理，用于修正纯 form 预测偏差。"
            )
        else:
            notes.append("赔率缺失时已自动退化为 form-only 方案，不依赖外部盘口也能输出结果。")

        seen: set[str] = set()
        deduplicated: list[str] = []
        for note in notes:
            key = note.strip()
            if key and key not in seen:
                seen.add(key)
                deduplicated.append(key)
        return deduplicated

    def _build_provider_evidence(
        self,
        snapshot: MatchSnapshot,
        odds_probability: ProbabilitySet | None,
    ) -> list[str]:
        evidence = list(snapshot.provider_evidence)
        evidence.append(
            f"home_form(last5={snapshot.home_form.last_5}, ppm={snapshot.home_form.points_per_match:.2f}, gf={snapshot.home_form.goals_for_avg:.2f}, ga={snapshot.home_form.goals_against_avg:.2f})"
        )
        evidence.append(
            f"away_form(last5={snapshot.away_form.last_5}, ppm={snapshot.away_form.points_per_match:.2f}, gf={snapshot.away_form.goals_for_avg:.2f}, ga={snapshot.away_form.goals_against_avg:.2f})"
        )
        evidence.append(f"injuries_count={len(snapshot.injuries)}")
        if snapshot.odds:
            evidence.append(
                f"odds_source={snapshot.odds.source or 'unknown'} prices={len(snapshot.odds.prices)} last_update={snapshot.odds.last_update or 'n/a'}"
            )
        if odds_probability:
            evidence.append(
                f"implied_probabilities=home:{odds_probability.home:.3f},draw:{odds_probability.draw:.3f},away:{odds_probability.away:.3f}"
            )
        else:
            evidence.append("implied_probabilities=unavailable(form_only_mode=true)")
        return evidence

    def _calculate_confidence_score(
        self,
        snapshot: MatchSnapshot,
        odds_probability: ProbabilitySet | None,
        final_probability: ProbabilitySet,
        home: TeamMetrics,
        away: TeamMetrics,
    ) -> float:
        score = 52.0
        score += min(len(snapshot.home_form.last_5), 5) * 2.0
        score += min(len(snapshot.away_form.last_5), 5) * 2.0
        score += 10.0 if odds_probability else -6.0
        score += min(len(snapshot.provider_evidence), 5) * 2.0
        score -= min(len(snapshot.injuries), 6) * 1.5

        confidence_gap = max(
            final_probability.home,
            final_probability.draw,
            final_probability.away,
        ) - sorted(
            [final_probability.home, final_probability.draw, final_probability.away],
            reverse=True,
        )[1]
        score += min(confidence_gap * 100 * 0.35, 10.0)
        score += min(abs(home.form_score - away.form_score) * 0.12, 8.0)

        return round(max(min(score, 92.0), 35.0), 2)
