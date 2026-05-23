"""捷报比分 (nowscore.com) 数据抓取器 —— 实时比分 + 事件 + 赔率 + WebSocket 推送

数据端点侦察结果:
  - 比赛列表: live.nowscore.com 主页 (JS 动态渲染)
  - 实时推送: wss://ws.nowscore.com/stream (change_xml channel, gzip)
  - 赔率数据: /odds/3in1Odds.aspx?id={match_id}
  - 比赛详情: /data/matchInfo.aspx?id={match_id}
  - 事件数据: /data/detail.js?{ts}  (^ 分隔, 11 字段)
  - 交锋历史: /data/panlu.js?{ts}   (数组格式)
  - 球队别名: /data/alias2.js?{ts}
  - 推荐比赛: /data/GetRecommend.aspx (JSON 数组)

change_xml 字段映射 (26 字段, ^ 分隔):
  [0]=match_id, [1]=status, [2]=?, [3]=home_score, [4]=away_score,
  [5]=half_home, [6]=half_away, [7]=home_red, [8]=away_red,
  [9]=minute(HH:MM), [10]=timestamp, [14]=date(MM-DD)
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
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

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────
BASE_URL = "https://live.nowscore.com"
WS_URL = "wss://ws.nowscore.com/stream"
TOKEN_URL = f"{BASE_URL}/Special/GetSocketToken"
ODDS_URL = f"{BASE_URL}/odds/3in1Odds.aspx"
MATCH_INFO_URL = f"{BASE_URL}/data/matchInfo.aspx"
DETAIL_JS_URL = f"{BASE_URL}/data/detail.js"
PANLU_JS_URL = f"{BASE_URL}/data/panlu.js"
ALIAS_JS_URL = f"{BASE_URL}/data/alias2.js"
RECOMMEND_URL = f"{BASE_URL}/data/GetRecommend.aspx"
CONDITION_URL = f"{BASE_URL}/data/condition.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{BASE_URL}/",
}

# 状态码映射 (从 change_xml [1] 字段)
STATUS_MAP: dict[str, str] = {
    "0": "SCHEDULED",   # 未开始
    "1": "LIVE",        # 上半场
    "2": "HT",          # 中场
    "3": "LIVE",        # 下半场
    "4": "FINISHED",    # 已结束
    "-1": "CANCELLED",  # 取消
}


class NowscoreProvider(BaseProvider):
    """捷报比分数据提供方。

    支持:
      - HTTP 轮询获取比赛列表 + 赔率
      - WebSocket 实时推送比分/事件 (change_xml channel)
    """

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._ws: Any = None
        self._ws_token: str | None = None
        self._live_callbacks: list[Any] = []
        self._alias_cache: dict[str, str] = {}

    # ── BaseProvider 接口 ─────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return True  # 捷报比分免费可用

    async def get_upcoming_matches(self, date: str | None = None) -> list[MatchRef]:
        """从捷报比分获取比赛列表。

        策略:
          1. 先尝试 GetRecommend.aspx 获取推荐比赛 ID 列表
          2. 逐个获取 matchInfo.aspx 获取详情
          3. 如果失败，回退到解析主页 HTML
        """
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=True,
                                         headers=HEADERS) as client:
                # 获取推荐比赛 ID
                r = await client.get(RECOMMEND_URL)
                if r.status_code != 200:
                    return await self._parse_main_page(client, date)

                try:
                    match_ids = json.loads(r.text)
                except json.JSONDecodeError:
                    return await self._parse_main_page(client, date)

                if not match_ids:
                    return await self._parse_main_page(client, date)

                # 批量获取比赛详情
                matches: list[MatchRef] = []
                # 限制数量避免请求过多
                for mid in match_ids[:50]:
                    try:
                        match = await self._fetch_match_info(client, str(mid))
                        if match:
                            if date and match.kickoff:
                                match_date = match.kickoff[:10]
                                if match_date != date:
                                    continue
                            matches.append(match)
                    except Exception:
                        continue

                return matches
        except Exception as exc:
            logger.warning("nowscore: 获取比赛列表失败: %s", exc)
            return []

    async def get_match_snapshot(self, match_id: str) -> MatchSnapshot:
        """获取比赛快照，包含近期战绩 + 交锋历史。"""
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=True,
                                         headers=HEADERS) as client:
                # 获取比赛基本信息
                match = await self._fetch_match_info(client, match_id)
                if not match:
                    return self.build_demo_snapshot(match_id)

                # 获取交锋历史 (从 panlu.js)
                h2h = await self._fetch_h2h(client, match_id)

                # 构建 TeamForm
                home_form = TeamForm(
                    recent_results=[],
                    recent_scores=[],
                    rank_info="",
                )
                away_form = TeamForm(
                    recent_results=[],
                    recent_scores=[],
                    rank_info="",
                )

                return MatchSnapshot(
                    match_id=match_id,
                    home_team=match.home_team,
                    away_team=match.away_team,
                    kickoff=match.kickoff,
                    league=match.league,
                    status=match.status,
                    home_score=match.home_score,
                    away_score=match.away_score,
                    minute=match.minute,
                    home_form=home_form,
                    away_form=away_form,
                    h2h_summary=h2h,
                    injuries=[],
                    odds=None,
                    provider="nowscore",
                )
        except Exception as exc:
            logger.warning("nowscore: 获取比赛快照失败 match_id=%s: %s", match_id, exc)
            return self.build_demo_snapshot(match_id)

    async def get_match_odds(self, match_id: str) -> OddsSnapshot:
        """从 3in1Odds.aspx 提取赔率数据。"""
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=True,
                                         headers=HEADERS) as client:
                return await self._fetch_odds(client, match_id)
        except Exception as exc:
            logger.warning("nowscore: 获取赔率失败 match_id=%s: %s", match_id, exc)
            return OddsSnapshot(match_id=match_id, prices=[], provider="nowscore")

    # ── 内部方法 ──────────────────────────────────────

    async def _parse_main_page(self, client: httpx.AsyncClient, date: str | None) -> list[MatchRef]:
        """解析主页 HTML 提取比赛列表 (回退方案)。"""
        r = await client.get(f"{BASE_URL}/")
        if r.status_code != 200:
            return []

        text = r.text
        matches: list[MatchRef] = []

        # 主页数据通过 JS 动态加载，尝试从内联 script 提取
        # 找包含 match ID 的数组
        id_pattern = re.findall(r'"(\d{6,8})"', text)
        seen: set[str] = set()

        for mid in id_pattern[:30]:
            if mid in seen:
                continue
            seen.add(mid)
            try:
                match = await self._fetch_match_info(client, mid)
                if match:
                    if date and match.kickoff:
                        if match.kickoff[:10] != date:
                            continue
                    matches.append(match)
            except Exception:
                continue

        return matches

    async def _fetch_match_info(self, client: httpx.AsyncClient, match_id: str) -> MatchRef | None:
        """从 matchInfo.aspx 提取单场比赛信息。"""
        url = f"{MATCH_INFO_URL}?id={match_id}"
        r = await client.get(url)
        if r.status_code != 200:
            return None

        raw = r.content
        try:
            text = raw.decode("gbk")
        except Exception:
            text = raw.decode("utf-8", errors="replace")

        soup = BeautifulSoup(text, "html.parser")

        # 移除 script/style 标签，防止 JS 代码污染字段提取
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()

        # 提取联赛
        league = ""
        league_el = soup.find("a", href=re.compile(r"league|ls"))
        if league_el:
            league = league_el.get_text(strip=True)

        # 提取主队/客队
        home_name = ""
        away_name = ""
        team_links = soup.find_all("a", href=re.compile(r"team"))
        if len(team_links) >= 2:
            home_name = team_links[0].get_text(strip=True)
            away_name = team_links[1].get_text(strip=True)

        # 提取比分
        home_score: int | None = None
        away_score: int | None = None
        score_el = soup.find("span", class_=re.compile(r"score|red|bf"), id=re.compile(r"score|bf"))
        if not score_el:
            score_el = soup.find("div", class_=re.compile(r"score|redf"))
        if score_el:
            score_text = score_el.get_text(strip=True)
            score_match = re.match(r"(\d+)\s*[-:]\s*(\d+)", score_text)
            if score_match:
                home_score = int(score_match.group(1))
                away_score = int(score_match.group(2))

        # 提取比赛时间
        kickoff = ""
        minute: int | None = None
        status = "SCHEDULED"

        time_el = soup.find(string=re.compile(r"\d{2}:\d{2}"))
        if time_el:
            time_str = time_el.strip()
            if re.match(r"\d{2}:\d{2}$", time_str):
                # 可能是比赛分钟数
                parts = time_str.split(":")
                minute = int(parts[0])
                status = "LIVE"

        # 提取开赛时间（仅匹配纯日期时间字符串，排除 JS 代码中的日期）
        date_el = soup.find(string=re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{2}:\d{2}"))
        if not date_el:
            date_el = soup.find(string=re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$"))
        if date_el:
            raw_date = date_el.strip()
            # 只取前 19 个字符（ISO 格式），防止尾部混入垃圾
            if len(raw_date) > 19:
                raw_date = raw_date[:19]
            kickoff = raw_date

        # 提取状态文本
        status_el = soup.find(string=re.compile(r"(进行中|已结束|未开始|中场|完场|推迟|取消)"))
        if status_el:
            status_text = status_el.strip()
            if "进行中" in status_text or "上半" in status_text:
                status = "LIVE"
            elif "中场" in status_text:
                status = "HT"
            elif "下半" in status_text:
                status = "LIVE"
            elif "已结束" in status_text or "完场" in status_text:
                status = "FINISHED"
            elif "推迟" in status_text:
                status = "POSTPONED"
            elif "取消" in status_text:
                status = "CANCELLED"

        if not home_name or not away_name:
            return None

        return MatchRef(
            id=f"nowscore-{match_id}",
            home_team=TeamRef(name=home_name),
            away_team=TeamRef(name=away_name),
            kickoff=kickoff or None,
            league=league or None,
            status=status,
            home_score=home_score,
            away_score=away_score,
            minute=minute,
            live_status=status,
        )

    async def _fetch_h2h(self, client: httpx.AsyncClient, match_id: str) -> list[str]:
        """从 panlu.js 提取交锋历史摘要。"""
        try:
            ts = int(datetime.now(timezone.utc).timestamp())
            r = await client.get(f"{PANLU_JS_URL}?{ts}")
            raw = r.content
            try:
                text = raw.decode("gbk")
            except Exception:
                text = raw.decode("utf-8", errors="replace")

            # 查找包含该 match_id 的记录
            # panlu.js 格式: p[n]=['球队A','#color','date',id1,id2,...]
            h2h_lines: list[str] = []
            pattern = re.compile(r"p\[\d+\]=\[([^\]]+)\]")
            for m in pattern.finditer(text):
                content = m.group(1)
                if match_id in content:
                    # 提取日期和比分
                    parts = content.split(",")
                    if len(parts) >= 9:
                        date_str = parts[2].strip("'")
                        home_goals = parts[5].strip()
                        away_goals = parts[6].strip()
                        h2h_lines.append(f"{date_str}: {home_goals}-{away_goals}")

            return h2h_lines[:10]
        except Exception:
            return []

    async def _fetch_odds(self, client: httpx.AsyncClient, match_id: str) -> OddsSnapshot:
        """从 3in1Odds.aspx 提取赔率数据。

        页面包含 3 个 <table class="gts">:
          Table 0 — 亚盘: 时间 | 比分 | 主 | 盘口 | 客 | 变化 | 状
          Table 1 — 大小球: 时间 | 比分 | 大 | 盘口 | 小 | 变化 | 状
          Table 2 — 欧赔: 时间 | 比分 | 主胜 | 和局 | 客胜 | 变化 | 状

        每行 <tr class="gt1|gt2"> 代表一次赔率变化记录，
        第一行 gt1 为最新赔率。
        """
        url = f"{ODDS_URL}?id={match_id}"
        r = await client.get(url)
        if r.status_code != 200:
            return OddsSnapshot(match_id=match_id, prices=[], source="nowscore")

        text = r.text
        soup = BeautifulSoup(text, "html.parser")
        prices: list[OddsPrice] = []

        tables = soup.find_all("table", class_="gts")
        if len(tables) < 3:
            return OddsSnapshot(match_id=match_id, prices=[], source="nowscore")

        # ── Table 0: 亚盘 ──
        asian_rows = tables[0].find_all("tr", class_=re.compile(r"gt[12]"))
        if asian_rows:
            tds = asian_rows[0].find_all("td")
            if len(tds) >= 5:
                try:
                    home_water = float(tds[2].get_text(strip=True))
                    handicap_text = tds[3].get_text(strip=True)
                    away_water = float(tds[4].get_text(strip=True))
                    line = self._parse_handicap_line(handicap_text)

                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="asian_handicap",
                        selection="home",
                        odds=home_water,
                        line=line,
                    ))
                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="asian_handicap",
                        selection="away",
                        odds=away_water,
                        line=line,
                    ))
                except (ValueError, IndexError):
                    pass

        # ── Table 1: 大小球 ──
        totals_rows = tables[1].find_all("tr", class_=re.compile(r"gt[12]"))
        if totals_rows:
            tds = totals_rows[0].find_all("td")
            if len(tds) >= 5:
                try:
                    over_odds = float(tds[2].get_text(strip=True))
                    totals_text = tds[3].get_text(strip=True)
                    under_odds = float(tds[4].get_text(strip=True))
                    totals_line = self._parse_handicap_line(totals_text)

                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="over_under",
                        selection="over",
                        odds=over_odds,
                        line=totals_line,
                    ))
                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="over_under",
                        selection="under",
                        odds=under_odds,
                        line=totals_line,
                    ))
                except (ValueError, IndexError):
                    pass

        # ── Table 2: 欧赔 ──
        euro_rows = tables[2].find_all("tr", class_=re.compile(r"gt[12]"))
        if euro_rows:
            tds = euro_rows[0].find_all("td")
            if len(tds) >= 5:
                try:
                    home_win = float(tds[2].get_text(strip=True))
                    draw = float(tds[3].get_text(strip=True))
                    away_win = float(tds[4].get_text(strip=True))

                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="1x2",
                        selection="home",
                        odds=home_win,
                    ))
                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="1x2",
                        selection="draw",
                        odds=draw,
                    ))
                    prices.append(OddsPrice(
                        bookmaker="nowscore",
                        market="1x2",
                        selection="away",
                        odds=away_win,
                    ))
                except (ValueError, IndexError):
                    pass

        return OddsSnapshot(match_id=match_id, prices=prices, source="nowscore")

    @staticmethod
    def _parse_handicap_line(text: str) -> float | None:
        """解析盘口文本为 float line。

        支持格式: "0", "0/0.5", "0.5", "1", "1/1.5", "1.5", "-0.5", "受0.5" 等
        """
        if not text:
            return None

        # 处理 "受让" 前缀 (表示负盘)
        is_reverse = "受" in text
        text = text.replace("受", "").replace("让", "").strip()

        # 映射常见盘口
        handicap_map: dict[str, float] = {
            "0": 0.0,
            "0/0.5": 0.25,
            "0.5": 0.5,
            "0.5/1": 0.75,
            "1": 1.0,
            "1/1.5": 1.25,
            "1.5": 1.5,
            "1.5/2": 1.75,
            "2": 2.0,
            "2/2.5": 2.25,
            "2.5": 2.5,
            "2.5/3": 2.75,
            "3": 3.0,
            "3/3.5": 3.25,
            "3.5": 3.5,
            "3.5/4": 3.75,
            "4": 4.0,
        }

        if text in handicap_map:
            result = handicap_map[text]
            return -result if is_reverse else result

        # 尝试直接解析数字
        try:
            result = float(text)
            return -result if is_reverse else result
        except ValueError:
            pass

        return None

    # ── WebSocket 实时数据 ────────────────────────────

    async def _get_ws_token(self) -> str:
        """获取 WebSocket JWT Token。"""
        if self._ws_token:
            # 简单检查是否过期 (不严格验证)
            return self._ws_token

        async with httpx.AsyncClient(timeout=10, trust_env=False, headers=HEADERS) as client:
            r = await client.get(TOKEN_URL)
            if r.status_code == 200:
                self._ws_token = r.text.strip()
                return self._ws_token

        raise RuntimeError("无法获取 WebSocket Token")

    async def connect_live_stream(self, callback: Any) -> None:
        """连接 WebSocket 实时推送流。

        callback 签名: async def on_live_data(matches: list[dict])
        其中每个 dict 包含: match_id, status, home_score, away_score,
                            half_home, half_away, home_red, away_red, minute
        """
        self._live_callbacks.append(callback)

        if self._ws is not None:
            return  # 已连接

        token = await self._get_ws_token()
        ws_url = f"{WS_URL}?token={token}"

        # 在后台任务中运行 WebSocket 连接
        asyncio.create_task(self._ws_loop(ws_url))

    async def _ws_loop(self, ws_url: str) -> None:
        """WebSocket 主循环。"""
        try:
            import websockets
        except ImportError:
            logger.error("nowscore: 需要安装 websockets 库: pip install websockets")
            return

        try:
            async with websockets.connect(
                ws_url,
                extra_headers={"User-Agent": HEADERS["User-Agent"]},
                ping_interval=30,
                close_timeout=5,
                open_timeout=10,
            ) as ws:
                self._ws = ws

                # 订阅 change_xml channel
                sub_msg = json.dumps({"type": 0, "channels": ["change_xml"]})
                await ws.send(sub_msg)
                resp = await ws.recv()
                logger.info("nowscore: WebSocket 订阅成功: %s", resp)

                # 接收推送
                while True:
                    try:
                        msg = await ws.recv()
                        if isinstance(msg, bytes):
                            # gzip 解压
                            try:
                                decompressed = gzip.decompress(msg)
                                data = json.loads(decompressed.decode("utf-8"))
                                parsed = self._parse_change_xml(data.get("change_xml", ""))
                                if parsed:
                                    for cb in self._live_callbacks:
                                        try:
                                            if asyncio.iscoroutinefunction(cb):
                                                await cb(parsed)
                                            else:
                                                cb(parsed)
                                        except Exception:
                                            pass
                            except Exception as exc:
                                logger.warning("nowscore: 解析 WebSocket 数据失败: %s", exc)
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        logger.warning("nowscore: WebSocket 接收异常: %s", exc)
                        break
        except Exception as exc:
            logger.warning("nowscore: WebSocket 连接失败: %s", exc)
        finally:
            self._ws = None
            # 5秒后重连
            await asyncio.sleep(5)
            if self._live_callbacks:
                asyncio.create_task(self._ws_loop(ws_url))

    def _parse_change_xml(self, raw: str) -> list[dict[str, Any]]:
        """解析 change_xml 数据。

        格式: match1^field1^field2^...!match2^field1^...
        字段映射 (26 字段):
          [0]=match_id, [1]=status_code, [2]=?,
          [3]=home_score, [4]=away_score,
          [5]=half_home, [6]=half_away,
          [7]=home_red, [8]=away_red,
          [9]=minute(HH:MM), [10]=timestamp,
          [14]=date(MM-DD)
        """
        if not raw:
            return []

        results: list[dict[str, Any]] = []
        for match_str in raw.split("!"):
            if not match_str.strip():
                continue
            fields = match_str.split("^")
            if len(fields) < 10:
                continue

            try:
                match_id = fields[0]
                status_code = fields[1]
                home_score = int(fields[3]) if fields[3] else 0
                away_score = int(fields[4]) if fields[4] else 0
                half_home = int(fields[5]) if fields[5] else None
                half_away = int(fields[6]) if fields[6] else None
                home_red = int(fields[7]) if fields[7] else 0
                away_red = int(fields[8]) if fields[8] else 0

                # 解析分钟数
                minute: int | None = None
                time_str = fields[9] if len(fields) > 9 else ""
                if ":" in time_str:
                    parts = time_str.split(":")
                    minute = int(parts[0])

                status = STATUS_MAP.get(status_code, "UNKNOWN")

                results.append({
                    "match_id": match_id,
                    "status": status,
                    "home_score": home_score,
                    "away_score": away_score,
                    "half_home": half_home,
                    "half_away": half_away,
                    "home_red": home_red,
                    "away_red": away_red,
                    "minute": minute,
                })
            except (ValueError, IndexError) as exc:
                logger.debug("nowscore: 解析单条 change_xml 失败: %s", exc)
                continue

        return results

    async def disconnect_live_stream(self) -> None:
        """断开 WebSocket 连接。"""
        self._live_callbacks.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None