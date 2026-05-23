"""500.com 竞彩足球数据抓取器 —— 逆向 trade.500.com/jczq/ 及详情页"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.models import (
    InjuryNote,
    MatchRef,
    MatchSnapshot,
    OddsPrice,
    OddsSnapshot,
    TeamForm,
    TeamRef,
)
from app.services.providers.base import BaseProvider

# ── 常量 ──────────────────────────────────────────────
JCZQ_URL = "https://trade.500.com/jczq/"
FENXI_BASE = "https://odds.500.com/fenxi/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 盘口 ref 值 → line 数值映射
HANDICAP_MAP: dict[str, float] = {
    "0.000": 0.0,
    "0.250": 0.25,
    "0.500": 0.5,
    "0.750": 0.75,
    "1.000": 1.0,
    "1.250": 1.25,
    "1.500": 1.5,
    "1.750": 1.75,
    "2.000": 2.0,
    "-0.250": -0.25,
    "-0.500": -0.5,
    "-0.750": -0.75,
    "-1.000": -1.0,
    "-1.250": -1.25,
    "-1.500": -1.5,
    "-1.750": -1.75,
    "-2.000": -2.0,
}


# ── 工具函数 ──────────────────────────────────────────
def _split_odds(text: str) -> list[float]:
    """从连在一起的赔率字符串中提取所有 X.XX 格式的数字。

    例如 "1.713.623.723.243.501.87" → [1.71, 3.62, 3.72, 3.24, 3.50, 1.87]
    """
    matches = re.findall(r"\d+\.\d{2}", text)
    return [float(m) for m in matches]


def _parse_handicap_ref(ref: str) -> float:
    """将盘口 ref 属性值转为 float line。"""
    if not ref:
        return 0.0
    try:
        return float(ref)
    except (ValueError, TypeError):
        return 0.0


def _normalize_result(text: str) -> str:
    """将中文赛果转为 W/D/L。"""
    t = text.strip()
    if t in ("胜", "赢"):
        return "W"
    if t in ("平", "走"):
        return "D"
    if t in ("负", "输"):
        return "L"
    return t


# ── Provider ──────────────────────────────────────────
class WubaiProvider(BaseProvider):
    """500.com 竞彩足球数据提供方。

    从 trade.500.com/jczq/ 抓取比赛列表，
    从 odds.500.com/fenxi/ 抓取详细分析数据。
    """

    name = "wubai"

    # ── 配置 ───────────────────────────────────────
    def is_configured(self) -> bool:
        """500.com 无需 API key，始终可用。"""
        return True

    # ── 比赛列表 ────────────────────────────────────
    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        """抓取 trade.500.com/jczq/ 获取所有比赛。"""
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                resp = await client.get(JCZQ_URL, headers=HEADERS, follow_redirects=True)
            resp.encoding = "gb2312"
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("tr.bet-tb-tr")
            matches: list[MatchRef] = []

            for row in rows:
                try:
                    match = self._parse_match_row(row, date)
                    if match:
                        matches.append(match)
                except Exception:
                    continue

            return matches

        except Exception:
            return []

    def _parse_match_row(self, row: Any, filter_date: str | None) -> MatchRef | None:
        """解析单个 bet-tb-tr 行。"""
        fixture_id = str(row.get("data-fixtureid", ""))
        match_date = str(row.get("data-matchdate", ""))
        match_time = str(row.get("data-matchtime", ""))
        home_name = str(row.get("data-homesxname", ""))
        away_name = str(row.get("data-awaysxname", ""))
        league = str(row.get("data-simpleleague", ""))
        home_id = str(row.get("data-homeid", ""))
        away_id = str(row.get("data-awayid", ""))
        match_num = str(row.get("data-matchnum", ""))

        if not fixture_id:
            return None

        # 日期过滤
        if filter_date and match_date != filter_date:
            return None

        kickoff = f"{match_date}T{match_time}:00" if match_date and match_time else ""

        return MatchRef(
            id=fixture_id,
            league=league,
            kickoff=kickoff,
            home_team=TeamRef(id=home_id, name=home_name),
            away_team=TeamRef(id=away_id, name=away_name),
            status="SCHEDULED",
            venue=match_num,
        )

    # ── 比赛快照 ────────────────────────────────────
    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        """抓取 shuju 页面获取交锋历史 + 近期战绩。"""
        try:
            url = f"{FENXI_BASE}shuju-{match_id}.shtml"
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                resp = await client.get(url, headers=HEADERS, follow_redirects=True)
            resp.encoding = "gb2312"
            soup = BeautifulSoup(resp.text, "html.parser")

            # 构建基础 MatchRef
            match_ref = self._extract_match_from_shuju(soup, match_id)

            # 提取交锋历史
            h2h_results = self._extract_h2h(soup, match_id)

            # 提取近期战绩
            home_form = self._extract_recent_form(soup, is_home=True)
            away_form = self._extract_recent_form(soup, is_home=False)

            # 提取积分排名信息
            rank_info = self._extract_rank_info(soup)

            notes: list[str] = []
            if rank_info:
                notes.append(rank_info)

            return MatchSnapshot(
                match=match_ref,
                home_form=home_form,
                away_form=away_form,
                injuries=[],
                weather=None,
                pre_match_notes=notes,
                odds=None,
                provider_evidence=[f"wubai:shuju-{match_id}"],
            )

        except Exception:
            return self.build_demo_snapshot(match_id)

    def _extract_match_from_shuju(self, soup: BeautifulSoup, match_id: str) -> MatchRef:
        """从 shuju 页面提取比赛基本信息。"""
        # 尝试从标题提取
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # 尝试从页面中的比赛信息区域提取
        # 查找包含 VS 的元素
        vs_elements = soup.find_all(string=re.compile(r"VS"))
        home_name = ""
        away_name = ""
        league = ""

        # 从 breadcrumb 或 header 提取
        for elem in soup.select(".M_title h4, .odds_hd h1, .match_info"):
            text = elem.get_text(strip=True)
            if "VS" in text or "vs" in text:
                parts = re.split(r"\s*VS\s*|\s*vs\s*", text, maxsplit=1)
                if len(parts) == 2:
                    home_name = parts[0].strip()
                    away_name = parts[1].strip()
                break

        # 从交锋历史表格的第一行（当前比赛行）提取
        current_row = soup.select_one(f'tr[fid="{match_id}"]')
        if current_row:
            league_td = current_row.select_one("td.td_one a")
            if league_td:
                league = league_td.get_text(strip=True)
            dz_td = current_row.select_one("td.dz")
            if dz_td:
                home_span = dz_td.select_one(".dz-l")
                away_span = dz_td.select_one(".dz-r")
                if home_span:
                    home_name = home_span.get_text(strip=True)
                if away_span:
                    away_name = away_span.get_text(strip=True)

        return MatchRef(
            id=match_id,
            league=league,
            kickoff="",
            home_team=TeamRef(id="", name=home_name),
            away_team=TeamRef(id="", name=away_name),
            status="SCHEDULED",
        )

    def _extract_h2h(self, soup: BeautifulSoup, current_match_id: str) -> list[str]:
        """提取交锋历史记录。"""
        results: list[str] = []
        h2h_rows = soup.select("tr[fid]")

        for row in h2h_rows:
            fid = str(row.get("fid", ""))
            if fid == current_match_id:
                continue  # 跳过当前比赛行

            result_td = row.select_one("td span.shu, td span.ying, td span.ping")
            if not result_td:
                # 尝试从赛果列获取
                result_cells = row.select("td")
                for cell in result_cells:
                    text = cell.get_text(strip=True)
                    if text in ("胜", "负", "平"):
                        results.append(_normalize_result(text))
                        break
                continue

            cls = result_td.get("class", [])
            if "ying" in str(cls):
                results.append("W")
            elif "shu" in str(cls):
                results.append("L")
            elif "ping" in str(cls):
                results.append("D")

        return results

    def _extract_recent_form(self, soup: BeautifulSoup, *, is_home: bool) -> TeamForm:
        """提取近期战绩。"""
        # 查找近期战绩表格
        # 页面中有两个战绩区域，分别对应主队和客队
        record_sections = soup.select(".record table")
        results: list[str] = []

        for section in record_sections:
            rows = section.select("tr")
            for row in rows:
                # 查找赛果
                result_spans = row.select("span.ying, span.shu, span.ping")
                for span in result_spans:
                    cls = span.get("class", [])
                    if "ying" in str(cls):
                        results.append("W")
                    elif "shu" in str(cls):
                        results.append("L")
                    elif "ping" in str(cls):
                        results.append("D")

        # 如果页面有两个战绩区域，分别取
        if is_home:
            form_results = results[:10] if len(results) > 10 else results[:5]
        else:
            mid = len(results) // 2
            form_results = results[mid : mid + 10] if len(results) > 20 else results[-5:]

        last_5 = form_results[-5:] if len(form_results) >= 5 else form_results

        # 计算统计数据
        wins = sum(1 for r in last_5 if r == "W")
        draws = sum(1 for r in last_5 if r == "D")
        points = wins * 3 + draws
        matches_count = max(len(last_5), 1)

        return TeamForm(
            last_5=last_5,
            points_per_match=round(points / matches_count, 2),
            goals_for_avg=0.0,
            goals_against_avg=0.0,
        )

    def _extract_rank_info(self, soup: BeautifulSoup) -> str:
        """提取积分排名信息。"""
        # 从页面标题或描述中提取排名
        for elem in soup.select(".M_title h4, .team_name strong"):
            text = elem.get_text(strip=True)
            if "排名" in text or "第" in text:
                return text

        # 从 header 信息提取
        for elem in soup.select(".odds_hd_info"):
            text = elem.get_text(strip=True)
            if text:
                return text

        return ""

    # ── 赔率数据 ────────────────────────────────────
    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        """抓取 yazhi + ouzhi 页面获取赔率数据。"""
        all_prices: list[OddsPrice] = []
        last_update = datetime.now(timezone.utc).isoformat()

        # 抓取亚盘
        try:
            yazhi_prices = await self._fetch_yazhi(match_id)
            all_prices.extend(yazhi_prices)
        except Exception:
            pass

        # 抓取欧赔
        try:
            ouzhi_prices = await self._fetch_ouzhi(match_id)
            all_prices.extend(ouzhi_prices)
        except Exception:
            pass

        if not all_prices:
            return self._build_demo_odds(match_id)

        return OddsSnapshot(
            match_id=match_id,
            prices=all_prices,
            last_update=last_update,
            source="wubai",
        )

    async def _fetch_yazhi(self, match_id: str) -> list[OddsPrice]:
        """抓取亚盘数据。"""
        url = f"{FENXI_BASE}yazhi-{match_id}.shtml"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
            resp = await client.get(url, headers=HEADERS, follow_redirects=True)
        resp.encoding = "gb2312"
        soup = BeautifulSoup(resp.text, "html.parser")

        prices: list[OddsPrice] = []
        rows = soup.select("#datatb tr.tr1, #datatb tr.tr2")

        for row in rows:
            try:
                # 博彩公司名
                company_td = row.select_one("td.tb_plgs")
                if not company_td:
                    continue
                company_name = company_td.get_text(strip=True)

                # 即时盘口
                instant_tables = row.select("td:nth-of-type(3) table.pl_table_data tr")
                if instant_tables:
                    self._parse_yazhi_row(
                        prices, company_name, instant_tables[0], "asian_handicap", "instant"
                    )

                # 初盘
                initial_tables = row.select("td:nth-of-type(5) table.pl_table_data tr")
                if initial_tables:
                    self._parse_yazhi_row(
                        prices, company_name, initial_tables[0], "asian_handicap", "initial"
                    )

            except Exception:
                continue

        return prices

    def _parse_yazhi_row(
        self,
        prices: list[OddsPrice],
        company: str,
        row: Any,
        market: str,
        period: str,
    ) -> None:
        """解析亚盘行数据。"""
        tds = row.find_all("td")
        if len(tds) < 3:
            return

        # 去除箭头等非数字后缀（如 "0.750↓" → "0.750"）
        home_text = re.sub(r"[^\d.]", "", tds[0].get_text(strip=True))
        away_text = re.sub(r"[^\d.]", "", tds[2].get_text(strip=True))
        home_water = self._safe_float(home_text)
        handicap_td = tds[1]
        away_water = self._safe_float(away_text)

        ref = handicap_td.get("ref", "")
        line = _parse_handicap_ref(ref)

        bookmaker = f"{company}({period})"

        prices.append(OddsPrice(
            bookmaker=bookmaker,
            market=market,
            selection="home",
            line=line,
            price=home_water,
        ))
        prices.append(OddsPrice(
            bookmaker=bookmaker,
            market=market,
            selection="away",
            line=line,
            price=away_water,
        ))

    async def _fetch_ouzhi(self, match_id: str) -> list[OddsPrice]:
        """抓取欧赔数据。"""
        url = f"{FENXI_BASE}ouzhi-{match_id}.shtml"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
            resp = await client.get(url, headers=HEADERS, follow_redirects=True)
        resp.encoding = "gb2312"
        soup = BeautifulSoup(resp.text, "html.parser")

        prices: list[OddsPrice] = []
        rows = soup.select("#datatb tr.tr1, #datatb tr.tr2")

        for row in rows:
            try:
                company_td = row.select_one("td.tb_plgs")
                if not company_td:
                    continue
                company_name = company_td.get_text(strip=True)

                # 赔率在 td:nth-of-type(3) 的 pl_table_data 中
                odds_tables = row.select("td:nth-of-type(3) table.pl_table_data")
                if not odds_tables:
                    continue

                # 第一行 tr_bdb 是即时赔率
                instant_rows = odds_tables[0].select("tr.tr_bdb")
                if instant_rows:
                    tds = instant_rows[0].find_all("td")
                    if len(tds) >= 3:
                        win_odds = self._safe_float(tds[0].get_text(strip=True))
                        draw_odds = self._safe_float(tds[1].get_text(strip=True))
                        lose_odds = self._safe_float(tds[2].get_text(strip=True))

                        bookmaker = f"{company_name}(instant)"
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="home",
                            line=None,
                            price=win_odds,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="draw",
                            line=None,
                            price=draw_odds,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="away",
                            line=None,
                            price=lose_odds,
                        ))

                # 第二行是初盘
                all_rows_in_table = odds_tables[0].find_all("tr")
                if len(all_rows_in_table) >= 2:
                    initial_row = all_rows_in_table[1]
                    tds = initial_row.find_all("td")
                    if len(tds) >= 3:
                        win_odds = self._safe_float(tds[0].get_text(strip=True))
                        draw_odds = self._safe_float(tds[1].get_text(strip=True))
                        lose_odds = self._safe_float(tds[2].get_text(strip=True))

                        bookmaker = f"{company_name}(initial)"
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="home",
                            line=None,
                            price=win_odds,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="draw",
                            line=None,
                            price=draw_odds,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=bookmaker,
                            market="1x2",
                            selection="away",
                            line=None,
                            price=lose_odds,
                        ))

            except Exception:
                continue

        return prices

    # ── Demo 回退 ───────────────────────────────────
    def _build_demo_odds(self, match_id: str) -> OddsSnapshot:
        """构建 demo 赔率数据。"""
        return OddsSnapshot(
            match_id=match_id,
            prices=[
                OddsPrice(bookmaker="500.com(demo)", market="1x2", selection="home", line=None, price=1.80),
                OddsPrice(bookmaker="500.com(demo)", market="1x2", selection="draw", line=None, price=3.50),
                OddsPrice(bookmaker="500.com(demo)", market="1x2", selection="away", line=None, price=4.00),
                OddsPrice(bookmaker="500.com(demo)", market="asian_handicap", selection="home", line=-0.5, price=0.90),
                OddsPrice(bookmaker="500.com(demo)", market="asian_handicap", selection="away", line=-0.5, price=0.94),
                OddsPrice(bookmaker="500.com(demo)", market="over_under", selection="over", line=2.5, price=0.88),
                OddsPrice(bookmaker="500.com(demo)", market="over_under", selection="under", line=2.5, price=0.92),
            ],
            last_update=datetime.now(timezone.utc).isoformat(),
            source="wubai-demo",
        )


__all__ = ["WubaiProvider"]