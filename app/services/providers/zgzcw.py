"""足彩网 (zgzcw.com) 数据抓取器 —— 从百家指数页抓取比赛列表、欧赔、亚盘与凯利数据。

数据源:
- 百家指数: plzx.zgzcw.com/bjzs/ (无 WAF，HTML 表格)
- 亚盘分析: fenxi.zgzcw.com/ (亚盘水位变化历史)
- 比分直播: live.zgzcw.com/jz/ (备用)
- 凯利指数: 基于百家欧赔自行计算
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.models import (
    MatchRef,
    MatchSnapshot,
    OddsPrice,
    OddsSnapshot,
    TeamForm,
    TeamRef,
)
from app.services.providers.base import BaseProvider

# ── 常量 ──────────────────────────────────────────────
BJZS_URL = "https://plzx.zgzcw.com/bjzs/"
LIVE_URL = "http://live.zgzcw.com/jz/"
FENXI_BASE = "http://fenxi.zgzcw.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 百家指数表格列索引 (0-based)
COL_MATCH_NUM = 1       # 编号，如 '竞彩001'
COL_LEAGUE = 2          # 联赛，如 '日职联'
COL_TIME = 3            # 时间，如 '05-23 13:00'
COL_TEAMS = 4           # 主队VS客队，如 '广岛三箭VS名古屋鲸八'
COL_INITIAL_WIN = 5     # 初盘胜
COL_INITIAL_DRAW = 6    # 初盘平
COL_INITIAL_LOSE = 7    # 初盘负
COL_INSTANT_WIN = 8     # 即时胜 (带箭头)
COL_INSTANT_DRAW = 9    # 即时平 (带箭头)
COL_INSTANT_LOSE = 10   # 即时负 (带箭头)
COL_TREND_TAG = 11      # 趋势标签，如 '欧赔'、'亚盘'


# ── 工具函数 ──────────────────────────────────────────
def _strip_arrow(text: str) -> tuple[float, str]:
    """去除箭头符号，返回 (数值, 方向)。

    例如:
        '1.81↑' → (1.81, 'up')
        '3.79↓' → (3.79, 'down')
        '1.81→' → (1.81, 'stable')
        '4.250'  → (4.25, '')

    兼容编码问题：如果箭头字符损坏，通过检查最后一个非数字字符判断方向。
    """
    t = text.strip()
    if not t:
        return 0.0, ""

    direction = ""
    # 先尝试精确匹配 Unicode 箭头
    if t.endswith("\u2191"):  # ↑
        direction = "up"
        t = t[:-1]
    elif t.endswith("\u2193"):  # ↓
        direction = "down"
        t = t[:-1]
    elif t.endswith("\u2192"):  # →
        direction = "stable"
        t = t[:-1]
    else:
        # 编码损坏时的回退：去掉末尾所有非数字非小数点字符
        stripped = t.rstrip(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            " \t\n\r"
            "\u2191\u2193\u2192"          # ↑ ↓ →
            "\u25b2\u25bc\u25b4\u25be"    # ▲ ▼ ▴ ▾
            "\u2191\ufe0f\u2193\ufe0f"    # ↑️ ↓️ (variation selector)
            "\u2b06\ufe0f\u2b07\ufe0f"    # ⬆️ ⬇️
            "\u00a0\xa0"                  # non-breaking spaces
        )
        if stripped != t:
            # 有非数字字符被去掉，尝试判断方向
            tail = t[len(stripped):]
            if any(c in tail for c in "\u2191\u25b2\u25b4\u2b06"):
                direction = "up"
            elif any(c in tail for c in "\u2193\u25bc\u25be\u2b07"):
                direction = "down"
            elif any(c in tail for c in "\u2192"):
                direction = "stable"
            t = stripped

    try:
        return float(t), direction
    except (ValueError, TypeError):
        return 0.0, direction


def _extract_match_id_from_href(href: str) -> str:
    """从链接中提取 match_id。

    例如:
        'http://fenxi.zgzcw.com/4470819/bjop' → 'zgzcw-4470819'
        '/4470819/bjop' → 'zgzcw-4470819'
    """
    m = re.search(r"/(\d{6,8})/", href)
    if m:
        return f"zgzcw-{m.group(1)}"
    return ""


def _parse_teams(teams_text: str) -> tuple[str, str]:
    """解析 '主队VS客队' 格式。

    例如:
        '广岛三箭VS名古屋鲸八' → ('广岛三箭', '名古屋鲸八')
        '广岛三箭 vs 名古屋鲸八' → ('广岛三箭', '名古屋鲸八')
    """
    parts = re.split(r"\s*VS\s*|\s*vs\s*", teams_text.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return teams_text.strip(), ""


# ── Provider ──────────────────────────────────────────
class ZgzcwProvider(BaseProvider):
    """足彩网数据提供方。

    从 plzx.zgzcw.com/bjzs/ 抓取百家指数比赛列表，
    包含欧赔初盘和即时盘数据。
    """

    name = "zgzcw"

    # ── 配置 ───────────────────────────────────────
    def is_configured(self) -> bool:
        """足彩网无需 API key，始终可用。"""
        return True

    # ── 比赛列表 ────────────────────────────────────
    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        """从百家指数页抓取所有比赛。"""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                resp = await client.get(BJZS_URL, headers=HEADERS, follow_redirects=True)

            # 中文网站，尝试多种编码
            encoding = self._detect_encoding(resp)
            resp.encoding = encoding
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("table tr")
            matches: list[MatchRef] = []

            for row in rows:
                try:
                    match = self._parse_bjzs_row(row, date)
                    if match:
                        matches.append(match)
                except Exception:
                    continue

            return matches

        except Exception:
            return []

    def _detect_encoding(self, resp: httpx.Response) -> str:
        """检测响应编码，优先从 meta 标签提取，默认 UTF-8。"""
        # 先尝试从 Content-Type header 获取
        content_type = resp.headers.get("content-type", "")
        m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
        if m:
            return m.group(1)

        # 从 HTML meta 标签检测
        meta_match = re.search(
            rb'<meta[^>]+charset=["\']?([\w-]+)',
            resp.content[:4096],
            re.IGNORECASE,
        )
        if meta_match:
            return meta_match.group(1).decode("ascii")

        # 默认 UTF-8（现代中文网站标准）
        return "utf-8"

    def _parse_bjzs_row(self, row: Any, filter_date: str | None) -> MatchRef | None:
        """解析百家指数表格中的一行。"""
        cells = row.find_all("td")
        if len(cells) < 12:
            return None

        # 提取各列文本
        col_texts = [cell.get_text(strip=True) for cell in cells]

        match_num = col_texts[COL_MATCH_NUM] if len(col_texts) > COL_MATCH_NUM else ""
        league = col_texts[COL_LEAGUE] if len(col_texts) > COL_LEAGUE else ""
        time_str = col_texts[COL_TIME] if len(col_texts) > COL_TIME else ""
        teams_text = col_texts[COL_TEAMS] if len(col_texts) > COL_TEAMS else ""

        # 跳过表头行
        if not match_num or match_num in ("编号", "赛事编号"):
            return None
        if not teams_text or "VS" not in teams_text and "vs" not in teams_text:
            return None

        # 提取 match_id（从链接中）
        match_id = ""
        for a_tag in cells[COL_MATCH_NUM].find_all("a") if len(cells) > COL_MATCH_NUM else []:
            href = a_tag.get("href", "")
            match_id = _extract_match_id_from_href(href)
            if match_id:
                break

        # 如果编号列没有链接，尝试从主客队列找
        if not match_id and len(cells) > COL_TEAMS:
            for a_tag in cells[COL_TEAMS].find_all("a"):
                href = a_tag.get("href", "")
                match_id = _extract_match_id_from_href(href)
                if match_id:
                    break

        # 如果还是没有，用编号作为 fallback
        if not match_id:
            match_id = f"zgzcw-{match_num}"

        # 解析主客队
        home_name, away_name = _parse_teams(teams_text)

        # 日期过滤
        if filter_date:
            # 百家指数的时间格式是 MM-DD HH:MM
            # filter_date 格式是 YYYY-MM-DD
            if time_str and len(time_str) >= 5:
                row_mmdd = time_str[:5]  # 'MM-DD'
                filter_mmdd = filter_date[5:]  # 'MM-DD'
                if row_mmdd != filter_mmdd:
                    return None

        # 构建 kickoff (ISO 格式)
        kickoff = self._build_kickoff(time_str)

        return MatchRef(
            id=match_id,
            league=league,
            kickoff=kickoff,
            home_team=TeamRef(id="", name=home_name),
            away_team=TeamRef(id="", name=away_name),
            status="SCHEDULED",
            venue=match_num,
        )

    def _build_kickoff(self, time_str: str) -> str:
        """将 'MM-DD HH:MM' 格式转为 ISO 时间字符串。"""
        if not time_str:
            return ""
        try:
            now = datetime.now()
            parts = time_str.strip().split()
            if len(parts) >= 2:
                mmdd = parts[0]  # 'MM-DD'
                hhmm = parts[1]  # 'HH:MM'
                month, day = mmdd.split("-")
                hour, minute = hhmm.split(":")
                year = now.year
                # 如果月份比当前月份小很多（跨年），年份+1
                if int(month) < now.month - 6:
                    year += 1
                dt = datetime(year, int(month), int(day), int(hour), int(minute))
                return dt.isoformat()
        except (ValueError, IndexError):
            pass
        return ""

    # ── 比赛快照 ────────────────────────────────────
    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        """返回比赛快照，包含从百家指数页提取的赔率数据。"""
        try:
            # 提取数字 ID
            numeric_id = match_id.replace("zgzcw-", "")

            # 重新抓取百家指数页获取该场比赛的赔率
            odds = await self.get_match_odds(match_id)

            # 构建基础 MatchRef
            match_ref = await self._find_match_in_bjzs(numeric_id)
            if match_ref is None:
                match_ref = self.build_demo_match(match_id)

            return MatchSnapshot(
                match=match_ref,
                home_form=TeamForm(),
                away_form=TeamForm(),
                injuries=[],
                weather=None,
                pre_match_notes=[
                    f"数据来源: 足彩网百家指数 (plzx.zgzcw.com/bjzs/)",
                    f"match_id={match_id}",
                ],
                odds=odds,
                provider_evidence=[
                    f"zgzcw:bjzs-{numeric_id}",
                    f"provider={self.name}",
                ],
            )

        except Exception:
            return self.build_demo_snapshot(match_id)

    async def _find_match_in_bjzs(self, numeric_id: str) -> MatchRef | None:
        """在百家指数页中查找指定 match_id 的比赛。"""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                resp = await client.get(BJZS_URL, headers=HEADERS, follow_redirects=True)
            encoding = self._detect_encoding(resp)
            resp.encoding = encoding
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("table tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 12:
                    continue

                # 检查链接中是否包含目标 ID
                for cell in cells:
                    for a_tag in cell.find_all("a"):
                        href = a_tag.get("href", "")
                        if numeric_id in href:
                            return self._parse_bjzs_row(row, None)

            return None
        except Exception:
            return None

    # ── 赔率数据 ────────────────────────────────────
    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        """从百家指数页提取欧赔 + 亚盘数据。

        百家指数页的表格直接包含初盘和即时盘欧赔，
        fenxi.zgzcw.com 补充亚盘详细水位。
        """
        numeric_id = match_id.replace("zgzcw-", "")
        last_update = datetime.now(timezone.utc).isoformat()

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                resp = await client.get(BJZS_URL, headers=HEADERS, follow_redirects=True)
            encoding = self._detect_encoding(resp)
            resp.encoding = encoding
            soup = BeautifulSoup(resp.text, "html.parser")

            rows = soup.select("table tr")
            prices: list[OddsPrice] = []

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 12:
                    continue

                # 检查是否为目标比赛
                found = False
                for cell in cells:
                    for a_tag in cell.find_all("a"):
                        href = a_tag.get("href", "")
                        if numeric_id in href:
                            found = True
                            break
                    if found:
                        break

                if not found:
                    continue

                # 解析该行的赔率数据
                col_texts = [cell.get_text(strip=True) for cell in cells]

                # 初盘赔率
                if len(col_texts) > COL_INITIAL_LOSE:
                    try:
                        initial_win = float(col_texts[COL_INITIAL_WIN])
                        initial_draw = float(col_texts[COL_INITIAL_DRAW])
                        initial_lose = float(col_texts[COL_INITIAL_LOSE])

                        prices.append(OddsPrice(
                            bookmaker="zgzcw-bjzs(initial)",
                            market="1x2",
                            selection="home",
                            line=None,
                            price=initial_win,
                        ))
                        prices.append(OddsPrice(
                            bookmaker="zgzcw-bjzs(initial)",
                            market="1x2",
                            selection="draw",
                            line=None,
                            price=initial_draw,
                        ))
                        prices.append(OddsPrice(
                            bookmaker="zgzcw-bjzs(initial)",
                            market="1x2",
                            selection="away",
                            line=None,
                            price=initial_lose,
                        ))
                    except (ValueError, IndexError):
                        pass

                # 即时盘赔率 (带箭头)
                if len(col_texts) > COL_INSTANT_LOSE:
                    try:
                        instant_win, win_dir = _strip_arrow(col_texts[COL_INSTANT_WIN])
                        instant_draw, draw_dir = _strip_arrow(col_texts[COL_INSTANT_DRAW])
                        instant_lose, lose_dir = _strip_arrow(col_texts[COL_INSTANT_LOSE])

                        # 构建带趋势标记的 bookmaker 名
                        _DIR_LABEL = {"up": "↑", "down": "↓", "stable": "→"}
                        trends = []
                        if win_dir:
                            trends.append(f"胜{_DIR_LABEL.get(win_dir, win_dir)}")
                        if draw_dir:
                            trends.append(f"平{_DIR_LABEL.get(draw_dir, draw_dir)}")
                        if lose_dir:
                            trends.append(f"负{_DIR_LABEL.get(lose_dir, lose_dir)}")
                        trend_suffix = f" [{', '.join(trends)}]" if trends else ""

                        prices.append(OddsPrice(
                            bookmaker=f"zgzcw-bjzs(instant){trend_suffix}",
                            market="1x2",
                            selection="home",
                            line=None,
                            price=instant_win,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=f"zgzcw-bjzs(instant){trend_suffix}",
                            market="1x2",
                            selection="draw",
                            line=None,
                            price=instant_draw,
                        ))
                        prices.append(OddsPrice(
                            bookmaker=f"zgzcw-bjzs(instant){trend_suffix}",
                            market="1x2",
                            selection="away",
                            line=None,
                            price=instant_lose,
                        ))
                    except (ValueError, IndexError):
                        pass

                # 找到目标行后即可退出
                break

            # ── 补充亚盘数据 ──────────────────────────
            try:
                asian_prices = await self._fetch_asian_detail(match_id)
                if asian_prices:
                    prices.extend(asian_prices)
                    logger.info(
                        "zgzcw asian handicap: %d prices for %s",
                        len(asian_prices), match_id,
                    )
            except Exception:
                logger.debug("zgzcw asian handicap fetch failed for %s", match_id)

            if not prices:
                return self._build_demo_odds(match_id)

            return OddsSnapshot(
                match_id=match_id,
                prices=prices,
                last_update=last_update,
                source="zgzcw-bjzs",
            )

        except Exception:
            return self._build_demo_odds(match_id)

    # ── 亚盘数据 ────────────────────────────────────

    async def _fetch_asian_detail(self, match_id: str) -> list[OddsPrice]:
        """从 fenxi.zgzcw.com 抓取亚盘详细水位变化历史。

        尝试 URL 模式:
          - http://fenxi.zgzcw.com/yazhi/{match_id}
          - http://fenxi.zgzcw.com/{match_id}/yazhi
        """
        numeric_id = match_id.replace("zgzcw-", "")
        prices: list[OddsPrice] = []

        urls_to_try = [
            f"{FENXI_BASE}yazhi/{numeric_id}",
            f"{FENXI_BASE}{numeric_id}/yazhi",
            f"{FENXI_BASE}match/{numeric_id}/yazhi",
        ]

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            trust_env=False,
        ) as client:
            for url in urls_to_try:
                try:
                    resp = await client.get(url, headers=HEADERS, follow_redirects=True)
                    if resp.status_code != 200:
                        continue

                    encoding = self._detect_encoding(resp)
                    resp.encoding = encoding
                    soup = BeautifulSoup(resp.text, "html.parser")

                    # 找亚盘表格
                    tables = soup.find_all("table")
                    for table in tables:
                        rows = table.find_all("tr")
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 4:
                                continue

                            try:
                                # 尝试提取: 公司 | 盘口 | 主水 | 客水
                                bookmaker = cells[0].get_text(strip=True)
                                if not bookmaker or bookmaker in ("公司", "博彩公司", "亚盘公司"):
                                    continue

                                handicap_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                                home_water = float(cells[2].get_text(strip=True)) if len(cells) > 2 else 0.0
                                away_water = float(cells[3].get_text(strip=True)) if len(cells) > 3 else 0.0

                                line = self._parse_handicap_line(handicap_text)

                                prices.append(OddsPrice(
                                    bookmaker=f"zgzcw-asian-{bookmaker}",
                                    market="asian_handicap",
                                    selection="home",
                                    line=line,
                                    price=home_water,
                                ))
                                prices.append(OddsPrice(
                                    bookmaker=f"zgzcw-asian-{bookmaker}",
                                    market="asian_handicap",
                                    selection="away",
                                    line=line,
                                    price=away_water,
                                ))
                            except (ValueError, IndexError):
                                continue

                    if prices:
                        break  # 成功获取数据，不再尝试其他 URL

                except Exception:
                    continue

        return prices

    @staticmethod
    def _parse_handicap_line(text: str) -> float | None:
        """解析盘口文本为 float line。

        支持: "平手", "平手/半球", "半球", "受平手/半球", "0", "0/0.5", "0.5" 等
        """
        if not text:
            return None

        is_reverse = "受" in text
        text = text.replace("受", "").replace("让", "").strip()

        handicap_map: dict[str, float] = {
            "平手": 0.0, "0": 0.0,
            "平手/半球": 0.25, "0/0.5": 0.25,
            "半球": 0.5, "0.5": 0.5,
            "半球/一球": 0.75, "0.5/1": 0.75,
            "一球": 1.0, "1": 1.0,
            "一球/球半": 1.25, "1/1.5": 1.25,
            "球半": 1.5, "1.5": 1.5,
            "球半/两球": 1.75, "1.5/2": 1.75,
            "两球": 2.0, "2": 2.0,
            "两球/两球半": 2.25, "2/2.5": 2.25,
            "两球半": 2.5, "2.5": 2.5,
            "两球半/三球": 2.75, "2.5/3": 2.75,
            "三球": 3.0, "3": 3.0,
        }

        if text in handicap_map:
            result = handicap_map[text]
            return -result if is_reverse else result

        try:
            result = float(text)
            return -result if is_reverse else result
        except ValueError:
            pass

        return None

    # ── Demo 回退 ───────────────────────────────────
    def _build_demo_odds(self, match_id: str) -> OddsSnapshot:
        """构建 demo 赔率数据。"""
        return OddsSnapshot(
            match_id=match_id,
            prices=[
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-initial)",
                    market="1x2",
                    selection="home",
                    line=None,
                    price=1.85,
                ),
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-initial)",
                    market="1x2",
                    selection="draw",
                    line=None,
                    price=3.60,
                ),
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-initial)",
                    market="1x2",
                    selection="away",
                    line=None,
                    price=3.90,
                ),
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-instant) [胜up, 平down, 负down]",
                    market="1x2",
                    selection="home",
                    line=None,
                    price=1.81,
                ),
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-instant) [胜up, 平down, 负down]",
                    market="1x2",
                    selection="draw",
                    line=None,
                    price=3.79,
                ),
                OddsPrice(
                    bookmaker="zgzcw-bjzs(demo-instant) [胜up, 平down, 负down]",
                    market="1x2",
                    selection="away",
                    line=None,
                    price=3.80,
                ),
            ],
            last_update=datetime.now(timezone.utc).isoformat(),
            source="zgzcw-demo",
        )


__all__ = ["ZgzcwProvider"]