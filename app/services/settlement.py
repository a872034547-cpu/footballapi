from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────

DB_PATH = Path(__file__).resolve().parent.parent.parent / "php-predict" / "data" / "predict.db"
DEFAULT_STAKE = 1000.0

FINISHED_KEYWORDS: list[str] = ["完", "FT", "Finished", "终", "结束", "全场"]

# 1x2 方向标准化映射
DIR_MAP_1X2: dict[str, str] = {
    "home": "home",
    "主胜": "home",
    "1": "home",
    "win": "home",
    "home_win": "home",
    "away": "away",
    "客胜": "away",
    "2": "away",
    "away_win": "away",
    "draw": "draw",
    "平": "draw",
    "x": "draw",
}


# ── 数据类 ────────────────────────────────────────────────


@dataclass(slots=True)
class SettleResult:
    """单条预测的结算结果"""

    prediction_id: int
    match_id: str
    market: str
    status: str  # hit / miss / void
    text: str
    profit: float = 0.0
    roi: float = 0.0


@dataclass(slots=True)
class BatchSettleResult:
    """批量结算汇总"""

    total: int = 0
    hit: int = 0
    miss: int = 0
    void: int = 0
    details: list[SettleResult] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        effective = self.total - self.void
        return (self.hit / effective * 100) if effective > 0 else 0.0


# ── 服务类 ────────────────────────────────────────────────


class SettlementService:
    """自动结算服务 — 管理预测结算逻辑与定时任务"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        stake: float = DEFAULT_STAKE,
    ) -> None:
        self._db_path = str(db_path or DB_PATH)
        self._stake = stake
        self._scheduler: AsyncIOScheduler | None = None

    # ── 数据库辅助 ──────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接（同步，在 executor 中使用）"""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def _run_in_executor(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """在线程池中运行同步 sqlite3 操作"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    # ── 完赛判断 ────────────────────────────────────────

    @staticmethod
    def is_match_finished(status: str | None) -> bool:
        """判断比赛状态是否表示已完赛"""
        if not status:
            return False
        status_lower = status.lower()
        for kw in FINISHED_KEYWORDS:
            if kw.lower() in status_lower:
                return True
        return False

    @staticmethod
    def parse_score(score_str: str | None) -> tuple[int | None, int | None]:
        """从比分字符串解析主客队进球，支持 '2:1' / '2-1' / '2 - 1'"""
        if not score_str:
            return None, None
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_str)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None, None

    # ── 单项结算方法 ────────────────────────────────────

    def settle_1x2(
        self,
        prediction_label: str,
        home_score: int,
        away_score: int,
    ) -> bool:
        """
        胜平负结算

        Args:
            prediction_label: 预测方向标签 (home/draw/away 及其变体)
            home_score: 主队进球
            away_score: 客队进球

        Returns:
            True = 命中, False = 未命中
        """
        if home_score > away_score:
            actual = "home"
        elif home_score < away_score:
            actual = "away"
        else:
            actual = "draw"

        normalized = DIR_MAP_1X2.get(prediction_label.lower().strip(), prediction_label.lower().strip())
        return normalized == actual

    def settle_over_under(
        self,
        prediction_label: str,
        home_score: int,
        away_score: int,
    ) -> bool:
        """
        大小球结算

        Args:
            prediction_label: 预测方向标签 (如 over_2.5 / 大2.5 / under_2.5)
            home_score: 主队进球
            away_score: 客队进球

        Returns:
            True = 命中, False = 未命中或走水
        """
        total = home_score + away_score
        label_lower = prediction_label.lower().strip()

        # 提取盘口线
        m = re.search(r"(\d+\.?\d*)", label_lower)
        if not m:
            return False
        line = float(m.group(1))

        is_over = ("over" in label_lower) or ("大" in label_lower)

        if total > line:
            return is_over
        elif total < line:
            return not is_over
        # total == line → 走水
        return False

    def settle_asian_handicap(
        self,
        prediction_label: str,
        home_score: int,
        away_score: int,
    ) -> bool:
        """
        亚盘结算

        Args:
            prediction_label: 预测方向标签 (如 home_-0.5 / away_+0.5)
            home_score: 主队进球
            away_score: 客队进球

        Returns:
            True = 命中, False = 未命中或走水
        """
        label_lower = prediction_label.lower().strip()

        # 提取盘口线（支持正负号）
        m = re.search(r"([-+]?\d+\.?\d*)", label_lower)
        if not m:
            return False
        line = float(m.group(1))

        is_away = ("away" in label_lower) or ("客" in label_lower)

        # scoreDiff = is_away ? (away - home - line) : (home + line - away)
        score_diff = (away_score - home_score - line) if is_away else (home_score + line - away_score)

        if score_diff > 0:
            return True
        elif score_diff < 0:
            return False
        # score_diff == 0 → 走水
        return False

    # ── 走水检测（用于 settle_match 中区分 miss/void） ──

    @staticmethod
    def _is_void_over_under(prediction_label: str, home_score: int, away_score: int) -> bool:
        """检测大小球是否走水"""
        total = home_score + away_score
        m = re.search(r"(\d+\.?\d*)", prediction_label.lower())
        if m:
            return total == float(m.group(1))
        return False

    @staticmethod
    def _is_void_asian_handicap(prediction_label: str, home_score: int, away_score: int) -> bool:
        """检测亚盘是否走水"""
        label_lower = prediction_label.lower()
        m = re.search(r"([-+]?\d+\.?\d*)", label_lower)
        if not m:
            return False
        line = float(m.group(1))
        is_away = ("away" in label_lower) or ("客" in label_lower)
        score_diff = (away_score - home_score - line) if is_away else (home_score + line - away_score)
        return score_diff == 0

    # ── 结果文本生成 ────────────────────────────────────

    @staticmethod
    def _result_text_1x2(
        prediction_label: str,
        home_score: int,
        away_score: int,
        is_hit: bool,
    ) -> str:
        if home_score > away_score:
            actual = "主胜"
        elif home_score < away_score:
            actual = "客胜"
        else:
            actual = "平局"
        mark = "✓" if is_hit else "✗"
        return f"{home_score}:{away_score} → 预测{prediction_label} {mark} (实际{actual})"

    @staticmethod
    def _result_text_over_under(
        prediction_label: str,
        home_score: int,
        away_score: int,
        is_hit: bool,
        is_void: bool,
    ) -> str:
        total = home_score + away_score
        m = re.search(r"(\d+\.?\d*)", prediction_label)
        line_str = m.group(1) if m else "?"
        if is_void:
            return f"{home_score}:{away_score} (总{total}) → 走水 (线{line_str})"
        mark = "✓" if is_hit else "✗"
        return f"{home_score}:{away_score} (总{total}) → 预测{prediction_label} {mark}"

    @staticmethod
    def _result_text_asian_handicap(
        prediction_label: str,
        home_score: int,
        away_score: int,
        is_hit: bool,
        is_void: bool,
    ) -> str:
        m = re.search(r"([-+]?\d+\.?\d*)", prediction_label)
        line_str = m.group(1) if m else "?"
        if is_void:
            return f"{home_score}:{away_score} (盘口{line_str}) → 走水"
        mark = "✓" if is_hit else "✗"
        return f"{home_score}:{away_score} (盘口{line_str}) → 预测{prediction_label} {mark}"

    # ── 盈亏计算 ────────────────────────────────────────

    def _calc_profit(self, hit_status: str, odds: float) -> tuple[float, float]:
        """计算盈亏和 ROI"""
        if hit_status == "hit":
            profit = self._stake * (odds - 1)
            roi = (profit / self._stake) * 100
        elif hit_status == "miss":
            profit = -self._stake
            roi = -100.0
        else:  # void
            profit = 0.0
            roi = 0.0
        return profit, roi

    # ── 单场比赛结算 ────────────────────────────────────

    def _settle_match_sync(self, match_id: str, match_data: dict[str, Any]) -> BatchSettleResult:
        """
        结算单场比赛的所有预测（同步，在 executor 中运行）

        Args:
            match_id: 比赛 ID
            match_data: 比赛数据，至少包含 home_score, away_score
        """
        home_score = match_data.get("home_score")
        away_score = match_data.get("away_score")

        # 尝试从 score 字段解析
        if home_score is None or away_score is None:
            score_str = match_data.get("score")
            if score_str:
                home_score, away_score = self.parse_score(score_str)

        if home_score is None or away_score is None:
            logger.warning("无法获取比分 match_id=%s", match_id)
            return BatchSettleResult()

        home_score = int(home_score)
        away_score = int(away_score)

        conn = self._get_conn()
        try:
            # 查询该比赛所有 pending 预测
            rows = conn.execute(
                "SELECT * FROM predictions WHERE match_id = ? AND status = 'pending'",
                (match_id,),
            ).fetchall()

            if not rows:
                return BatchSettleResult()

            result = BatchSettleResult()
            stmt_result = None
            stmt_update = None

            for row in rows:
                pred_id = row["id"]
                market = row["market"]
                direction = row["direction"]
                odds = float(row["odds"] or 1.9)

                # 结算
                if market == "1x2":
                    is_hit = self.settle_1x2(direction, home_score, away_score)
                    hit_status = "hit" if is_hit else "miss"
                    result_text = self._result_text_1x2(direction, home_score, away_score, is_hit)
                elif market == "over_under":
                    is_void = self._is_void_over_under(direction, home_score, away_score)
                    if is_void:
                        hit_status = "void"
                        is_hit = False
                    else:
                        is_hit = self.settle_over_under(direction, home_score, away_score)
                        hit_status = "hit" if is_hit else "miss"
                    result_text = self._result_text_over_under(direction, home_score, away_score, is_hit, is_void)
                elif market == "asian_handicap":
                    is_void = self._is_void_asian_handicap(direction, home_score, away_score)
                    if is_void:
                        hit_status = "void"
                        is_hit = False
                    else:
                        is_hit = self.settle_asian_handicap(direction, home_score, away_score)
                        hit_status = "hit" if is_hit else "miss"
                    result_text = self._result_text_asian_handicap(direction, home_score, away_score, is_hit, is_void)
                else:
                    hit_status = "void"
                    result_text = f"未知市场类型: {market}"

                profit, roi = self._calc_profit(hit_status, odds)

                # 写入 prediction_results
                if stmt_result is None:
                    stmt_result = conn.execute  # 直接用 execute，简单场景
                conn.execute(
                    """INSERT OR REPLACE INTO prediction_results
                       (prediction_id, hit_status, stake, profit, roi, result_text, settled_at)
                       VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (pred_id, hit_status, self._stake, profit, roi, result_text),
                )

                # 更新 predictions.status
                conn.execute(
                    "UPDATE predictions SET status = 'settled' WHERE id = ?",
                    (pred_id,),
                )

                detail = SettleResult(
                    prediction_id=pred_id,
                    match_id=match_id,
                    market=market,
                    status=hit_status,
                    text=result_text,
                    profit=profit,
                    roi=roi,
                )
                result.details.append(detail)
                result.total += 1
                if hit_status == "hit":
                    result.hit += 1
                elif hit_status == "miss":
                    result.miss += 1
                else:
                    result.void += 1

            conn.commit()
            return result
        finally:
            conn.close()

    async def settle_match(
        self,
        match_id: str,
        match_data: dict[str, Any],
    ) -> BatchSettleResult:
        """
        结算单场比赛的所有预测（异步入口）

        Args:
            match_id: 比赛 ID
            match_data: 比赛数据字典，需包含 home_score, away_score
        """
        return await self._run_in_executor(self._settle_match_sync, match_id, match_data)

    # ── 批量检查并结算 ──────────────────────────────────

    def _check_and_settle_sync(self) -> BatchSettleResult:
        """检查所有进行中比赛并结算已完赛的（同步）"""
        conn = self._get_conn()
        try:
            # 查询所有 pending 预测及其关联比赛数据
            rows = conn.execute("""
                SELECT p.*, m.home_team, m.away_team, m.status AS match_status,
                       m.home_score, m.away_score, m.score
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE p.status = 'pending'
                ORDER BY p.match_id
            """).fetchall()

            if not rows:
                return BatchSettleResult()

            # 按 match_id 分组，筛选已完赛的比赛
            match_groups: dict[str, list[sqlite3.Row]] = {}
            match_data_map: dict[str, dict[str, Any]] = {}
            for row in rows:
                mid = row["match_id"]
                status = row["match_status"] or ""
                score_str = row["score"] or ""

                # 检查是否完赛
                if not self.is_match_finished(status):
                    # 尝试从 score 字符串判断（有比分但状态未标记完赛）
                    h, a = self.parse_score(score_str)
                    if h is None or a is None:
                        continue  # 未完赛且无有效比分

                if mid not in match_groups:
                    match_groups[mid] = []
                    match_data_map[mid] = {
                        "home_score": row["home_score"],
                        "away_score": row["away_score"],
                        "score": score_str,
                        "status": status,
                    }
                match_groups[mid].append(row)

            if not match_groups:
                return BatchSettleResult()

            # 逐场比赛结算
            aggregate = BatchSettleResult()
            for mid, preds in match_groups.items():
                match_data = match_data_map[mid]
                # 确保有比分
                if match_data["home_score"] is None or match_data["away_score"] is None:
                    h, a = self.parse_score(match_data.get("score"))
                    if h is not None and a is not None:
                        match_data["home_score"] = h
                        match_data["away_score"] = a
                    else:
                        logger.warning("跳过 match_id=%s: 无有效比分", mid)
                        continue

                batch = self._settle_match_sync(mid, match_data)
                aggregate.total += batch.total
                aggregate.hit += batch.hit
                aggregate.miss += batch.miss
                aggregate.void += batch.void
                aggregate.details.extend(batch.details)

                if batch.total > 0:
                    logger.info(
                        "结算 match_id=%s: hit=%d miss=%d void=%d",
                        mid,
                        batch.hit,
                        batch.miss,
                        batch.void,
                    )

            conn.commit()
            return aggregate
        finally:
            conn.close()

    async def check_and_settle(self) -> BatchSettleResult:
        """
        检查所有进行中比赛，对已完赛的触发结算（异步入口）

        由 APScheduler 每 60 秒调用一次
        """
        logger.debug("定时结算检查开始...")
        result = await self._run_in_executor(self._check_and_settle_sync)
        if result.total > 0:
            logger.info(
                "本轮结算完成: total=%d hit=%d miss=%d void=%d hit_rate=%.1f%%",
                result.total,
                result.hit,
                result.miss,
                result.void,
                result.hit_rate,
            )
        return result

    # ── 调度器管理 ──────────────────────────────────────

    def start_scheduler(self) -> None:
        """启动 APScheduler 定时任务（每 60 秒执行一次结算检查）"""
        if self._scheduler is not None:
            logger.warning("调度器已在运行中")
            return

        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.check_and_settle,
            "interval",
            seconds=60,
            id="settlement_check",
            name="自动结算检查",
            misfire_grace_time=30,
        )
        self._scheduler.start()
        logger.info("自动结算调度器已启动 (每 60 秒)")

    def stop_scheduler(self) -> None:
        """停止定时任务"""
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("自动结算调度器已停止")


# ── 模块级单例 ────────────────────────────────────────────

_settlement_service: SettlementService | None = None


def get_settlement_service(
    db_path: str | Path | None = None,
    stake: float = DEFAULT_STAKE,
) -> SettlementService:
    """获取 SettlementService 单例"""
    global _settlement_service
    if _settlement_service is None:
        _settlement_service = SettlementService(db_path=db_path, stake=stake)
    return _settlement_service


def start_scheduler() -> None:
    """启动自动结算调度器（供 main.py startup 事件调用）"""
    get_settlement_service().start_scheduler()


def stop_scheduler() -> None:
    """停止自动结算调度器（供 main.py shutdown 事件调用）"""
    get_settlement_service().stop_scheduler()