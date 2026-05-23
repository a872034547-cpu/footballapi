from __future__ import annotations

from pydantic import BaseModel, Field


class SourceStatus(BaseModel):
    name: str = ""
    enabled: bool = False
    configured: bool = False
    detail: str = ""


class TeamRef(BaseModel):
    id: str = ""
    name: str = ""
    short_name: str | None = None


class MatchRef(BaseModel):
    id: str = ""
    league: str = ""
    kickoff: str = ""
    home_team: TeamRef = Field(default_factory=TeamRef)
    away_team: TeamRef = Field(default_factory=TeamRef)
    status: str = "SCHEDULED"
    venue: str | None = None
    # 实时比分字段（滚球/直播场景）
    home_score: int | None = None
    away_score: int | None = None
    minute: int | None = None
    live_status: str | None = None  # LIVE / HT / FT / SCHEDULED


class OddsPrice(BaseModel):
    bookmaker: str = ""
    market: str = ""
    selection: str = ""
    line: float | None = None
    price: float = 0.0


class OddsSnapshot(BaseModel):
    match_id: str = ""
    prices: list[OddsPrice] = Field(default_factory=list)
    last_update: str | None = None
    source: str = ""


class TeamForm(BaseModel):
    last_5: list[str] = Field(default_factory=list)
    points_per_match: float = 0.0
    goals_for_avg: float = 0.0
    goals_against_avg: float = 0.0


class InjuryNote(BaseModel):
    team: str = ""
    player: str = ""
    status: str = ""
    impact: str = "medium"


class MatchSnapshot(BaseModel):
    match: MatchRef = Field(default_factory=MatchRef)
    home_form: TeamForm = Field(default_factory=TeamForm)
    away_form: TeamForm = Field(default_factory=TeamForm)
    injuries: list[InjuryNote] = Field(default_factory=list)
    weather: str | None = None
    pre_match_notes: list[str] = Field(default_factory=list)
    odds: OddsSnapshot | None = None
    provider_evidence: list[str] = Field(default_factory=list)


class ScorePrediction(BaseModel):
    score: str = ""
    probability: float = 0.0


class MarketPrediction(BaseModel):
    label: str = ""
    probability: float = 0.0
    rationale: str = ""


# ── 新增模型 ──────────────────────────────────────────────


class LiveMatchStatus(BaseModel):
    """实时比赛状态（用于 /matches/live 端点）"""
    match_id: str = ""
    league: str = ""
    home_team: str = ""
    away_team: str = ""
    home_score: int = 0
    away_score: int = 0
    minute: int = 0
    status: str = "LIVE"  # LIVE / HT / FT
    kickoff: str = ""


class RollingPrediction(BaseModel):
    """滚球预测"""
    match_id: str = ""
    minute: int = 0
    current_score: str = ""  # "1-0"
    prediction_type: str = ""  # win_draw_lose / over_under / asian_handicap / next_goal
    label: str = ""  # home_win / over_2.5 / home_-0.5
    probability: float = 0.0
    rationale: str = ""
    confidence_level: str = "C"  # A / B / C / D


class PatternMatchResult(BaseModel):
    """历史模式匹配结果"""
    pattern_type: str = ""  # handicap_derivation / odds_implied / asian_water / totals
    description: str = ""
    similarity: float = 0.0  # 0-100 相似度
    evidence: list[str] = Field(default_factory=list)
    derived_line: float | None = None  # 推导出的盘口线
    market_line: float | None = None  # 市场实际盘口线
    conclusion: str = ""


class AnalysisResponse(BaseModel):
    match: MatchRef = Field(default_factory=MatchRef)
    confidence_score: float = 0.0
    benefit_factors: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    pre_match_notes: list[str] = Field(default_factory=list)
    predicted_scores: list[ScorePrediction] = Field(default_factory=list)
    win_draw_lose: list[MarketPrediction] = Field(default_factory=list)
    over_under: list[MarketPrediction] = Field(default_factory=list)
    asian_handicap: list[MarketPrediction] = Field(default_factory=list)
    provider_evidence: list[str] = Field(default_factory=list)
    # 新增字段
    rolling_predictions: list[RollingPrediction] = Field(default_factory=list)
    pattern_matches: list[PatternMatchResult] = Field(default_factory=list)
    overall_confidence_level: str = ""  # A / B / C / D


class PredictionRequest(BaseModel):
    match_id: str | None = None
    league: str | None = None
    kickoff: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    home_recent_form: list[str] = Field(default_factory=list)
    away_recent_form: list[str] = Field(default_factory=list)
    injuries: list[InjuryNote] = Field(default_factory=list)
    over_under_line: float = 2.5
    handicap_line: float = 0.0


class BookmakerKellyDetail(BaseModel):
    """单个博彩公司的凯利值详情"""
    bookmaker: str = ""
    home_kelly: float | None = None
    draw_kelly: float | None = None
    away_kelly: float | None = None
    over_kelly: float | None = None
    under_kelly: float | None = None
    asian_home_kelly: float | None = None
    asian_away_kelly: float | None = None


class KellyResult(BaseModel):
    """凯利指数计算结果"""
    match_id: str
    home_kelly: float | None = None
    draw_kelly: float | None = None
    away_kelly: float | None = None
    over_kelly: float | None = None
    under_kelly: float | None = None
    asian_home_kelly: float | None = None
    asian_away_kelly: float | None = None
    bookmaker_count: int = 0
    bookmaker_details: list[BookmakerKellyDetail] = Field(default_factory=list)
    source: str = "calculated"
