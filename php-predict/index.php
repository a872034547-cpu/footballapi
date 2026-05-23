<?php
/**
 * index.php — 足球预测展示页面
 *
 * 功能：
 *   - Hero 区域：ROI 统计仪表盘
 *   - 今日预测表格（1x2 / 大小球 / 亚盘）
 *   - 历史回溯统计
 *   - 自动刷新按钮
 */

declare(strict_types=1);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
init_db();
$db = db();

// ---------------------------------------------------------------------------
// 查询 ROI 汇总
// ---------------------------------------------------------------------------
$roiSummary = $db->querySingle("
    SELECT
        COUNT(*)                                    AS total,
        COALESCE(SUM(stake), 0)                     AS total_stake,
        COALESCE(SUM(profit), 0)                    AS total_profit,
        COALESCE(SUM(CASE WHEN hit_status = 'hit'  THEN 1 ELSE 0 END), 0) AS hits,
        COALESCE(SUM(CASE WHEN hit_status = 'miss' THEN 1 ELSE 0 END), 0) AS misses,
        COALESCE(SUM(CASE WHEN hit_status = 'void' THEN 1 ELSE 0 END), 0) AS voids
    FROM prediction_results
", true);

$totalResults  = (int) ($roiSummary['total']  ?? 0);
$totalStake    = (float)($roiSummary['total_stake'] ?? 0);
$totalProfit   = (float)($roiSummary['total_profit'] ?? 0);
$hits          = (int) ($roiSummary['hits']   ?? 0);
$misses        = (int) ($roiSummary['misses'] ?? 0);
$voids         = (int) ($roiSummary['voids']  ?? 0);

$effectiveTotal = $totalResults - $voids;
$hitRate        = $effectiveTotal > 0 ? ($hits / $effectiveTotal * 100) : 0;
$roiPct         = $totalStake > 0 ? ($totalProfit / $totalStake * 100) : 0;

// ---------------------------------------------------------------------------
// 按市场类型分组统计
// ---------------------------------------------------------------------------
$marketStats = [];
$marketQuery = $db->query("
    SELECT
        p.market,
        COUNT(*)                                              AS total,
        COALESCE(SUM(CASE WHEN pr.hit_status = 'hit'  THEN 1 ELSE 0 END), 0) AS hits,
        COALESCE(SUM(CASE WHEN pr.hit_status = 'miss' THEN 1 ELSE 0 END), 0) AS misses,
        COALESCE(SUM(CASE WHEN pr.hit_status = 'void' THEN 1 ELSE 0 END), 0) AS voids,
        COALESCE(SUM(pr.profit), 0)                           AS profit
    FROM predictions p
    JOIN prediction_results pr ON p.id = pr.prediction_id
    GROUP BY p.market
");
while ($row = $marketQuery->fetchArray(SQLITE3_ASSOC)) {
    $marketStats[] = $row;
}

// ---------------------------------------------------------------------------
// 查询今日预测
// ---------------------------------------------------------------------------
$today = date('Y-m-d');
$todayPreds = $db->query("
    SELECT p.*, m.home_team, m.away_team, m.league, m.kickoff, m.status AS match_status,
           m.home_score, m.away_score, m.score,
           pr.hit_status AS result_status, pr.profit, pr.result_text
    FROM predictions p
    JOIN matches m ON p.match_id = m.match_id
    LEFT JOIN prediction_results pr ON p.id = pr.prediction_id
    WHERE date(m.kickoff) = '{$today}'
       OR date(m.kickoff) <= '{$today}' AND p.status = 'pending'
    ORDER BY m.kickoff, p.market, p.confidence DESC
");

$predictions = [];
while ($row = $todayPreds->fetchArray(SQLITE3_ASSOC)) {
    $predictions[] = $row;
}

// ---------------------------------------------------------------------------
// 查询最近已结算预测（用于回溯展示）
// ---------------------------------------------------------------------------
$recentSettled = $db->query("
    SELECT p.*, m.home_team, m.away_team, m.league, m.kickoff,
           m.home_score, m.away_score, m.score,
           pr.hit_status, pr.profit, pr.roi, pr.result_text, pr.settled_at
    FROM predictions p
    JOIN matches m ON p.match_id = m.match_id
    JOIN prediction_results pr ON p.id = pr.prediction_id
    WHERE p.status = 'settled'
    ORDER BY pr.settled_at DESC
    LIMIT 50
");

$settledList = [];
while ($row = $recentSettled->fetchArray(SQLITE3_ASSOC)) {
    $settledList[] = $row;
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

function market_label(string $market): string
{
    return match ($market) {
        '1x2'             => '胜平负',
        'over_under'      => '大小球',
        'asian_handicap'  => '亚盘',
        default           => $market,
    };
}

function direction_badge(string $market, string $direction): string
{
    $lower = strtolower($direction);
    return match ($market) {
        '1x2' => match (true) {
            str_contains($lower, 'home')   => '<span class="badge-hit">主胜</span>',
            str_contains($lower, 'draw')   => '<span class="badge-pending">平局</span>',
            str_contains($lower, 'away')   => '<span class="badge-miss">客胜</span>',
            default                        => e($direction),
        },
        'over_under' => match (true) {
            str_contains($lower, 'over')   => '<span class="badge-hit">大球 ' . e($direction) . '</span>',
            str_contains($lower, 'under')  => '<span class="badge-miss">小球 ' . e($direction) . '</span>',
            default                        => e($direction),
        },
        'asian_handicap' => match (true) {
            str_contains($lower, 'home')   => '<span class="badge-hit">主 ' . e($direction) . '</span>',
            str_contains($lower, 'away')   => '<span class="badge-miss">客 ' . e($direction) . '</span>',
            default                        => e($direction),
        },
        default => e($direction),
    };
}

function status_badge(?string $status): string
{
    if ($status === null) return '<span class="badge-pending">待结算</span>';
    return match ($status) {
        'hit'  => '<span class="badge-hit">✓ 命中</span>',
        'miss' => '<span class="badge-miss">✗ 未中</span>',
        'void' => '<span class="badge-pending">~ 走水</span>',
        default => '<span class="badge-pending">' . e($status) . '</span>',
    };
}

function profit_class(?float $profit): string
{
    if ($profit === null) return '';
    return $profit > 0 ? 'text-green' : ($profit < 0 ? 'badge-miss' : 'text-muted');
}

function format_kickoff(?string $kickoff): string
{
    if (empty($kickoff)) return '-';
    $ts = strtotime($kickoff);
    if ($ts === false) return e($kickoff);
    return date('m-d H:i', $ts);
}

function display_score(?int $home, ?int $away): string
{
    if ($home === null || $away === null) return 'vs';
    return "{$home} : {$away}";
}

// ---------------------------------------------------------------------------
// 页面渲染
// ---------------------------------------------------------------------------
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>足球预测 — AI 智能分析</title>
    <link rel="stylesheet" href="style.css">
    <style>
        /* 页面特有补充样式 */
        .section-title {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }
        .section-title h2 {
            margin: 0;
        }
        .section-title .badge {
            font-size: 12px;
            padding: 4px 12px;
            border-radius: 999px;
            background: rgba(125, 244, 139, 0.12);
            color: #7df48b;
            font-weight: 700;
        }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #8ea3bd;
        }
        .empty-state .icon {
            font-size: 48px;
            margin-bottom: 16px;
        }
        .refresh-bar {
            display: flex;
            justify-content: flex-end;
            gap: 12px;
            margin-bottom: 20px;
        }
        .profit-positive { color: #7df48b; font-weight: 800; }
        .profit-negative { color: #ff6b6b; font-weight: 800; }
        .footer-note {
            text-align: center;
            padding: 40px 20px;
            color: #8ea3bd;
            font-size: 12px;
            border-top: 1px solid rgba(255,255,255,0.06);
            margin-top: 40px;
        }
        /* Tab 栏样式 */
        .tab-bar {
            display: flex;
            gap: 4px;
            margin-bottom: 16px;
            background: rgba(255,255,255,0.03);
            border-radius: 10px;
            padding: 4px;
            width: fit-content;
        }
        .tab-btn {
            padding: 8px 20px;
            border: none;
            border-radius: 8px;
            background: transparent;
            color: #8ea3bd;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .tab-btn:hover {
            color: #e0e6ed;
            background: rgba(255,255,255,0.05);
        }
        .tab-btn.active {
            background: rgba(125, 244, 139, 0.15);
            color: #7df48b;
            box-shadow: 0 0 12px rgba(125, 244, 139, 0.1);
        }
        .refresh-info {
            font-size: 12px;
            color: #8ea3bd;
            display: flex;
            align-items: center;
        }
        /* 比分脉冲动画 */
        @keyframes scorePulse {
            0%   { transform: scale(1);   color: #7df48b; }
            50%  { transform: scale(1.25); color: #ffd700; }
            100% { transform: scale(1);   color: #7df48b; }
        }
        .score-pulse {
            animation: scorePulse 0.6s ease-in-out;
        }
    </style>
</head>
<body>

<!-- ═══════════════════════════════════════════ -->
<!--  Hero 区域                                   -->
<!-- ═══════════════════════════════════════════ -->
<section class="hero">
    <div class="container">
        <h1>⚽ 足球<span>AI 预测</span></h1>
        <p>基于多数据源聚合 + 规则引擎，自动分析今日比赛，提供胜平负 / 大小球 / 亚盘预测</p>

        <!-- 统计仪表盘 -->
        <div class="stats-bar">
            <div class="stat-item">
                <span class="stat-value"><?= $totalResults ?></span>
                <span class="stat-label">总预测数</span>
            </div>
            <div class="stat-item">
                <span class="stat-value"><?= number_format($hitRate, 1) ?>%</span>
                <span class="stat-label">命中率</span>
            </div>
            <div class="stat-item">
                <span class="stat-value"><?= $totalProfit >= 0 ? '+' : '' ?><?= money_fmt($totalProfit) ?></span>
                <span class="stat-label">累计盈亏</span>
            </div>
            <div class="stat-item">
                <span class="stat-value <?= $roiPct >= 0 ? 'text-green' : '' ?>"><?= $roiPct >= 0 ? '+' : '' ?><?= number_format($roiPct, 1) ?>%</span>
                <span class="stat-label">ROI</span>
            </div>
        </div>

        <!-- 按市场细分 -->
        <?php if (!empty($marketStats)): ?>
        <div class="stats-bar" style="margin-top: 12px;">
            <?php foreach ($marketStats as $ms):
                $mTotal = (int)$ms['total'];
                $mHits  = (int)$ms['hits'];
                $mVoids = (int)$ms['voids'];
                $mEff   = max($mTotal - $mVoids, 1);
                $mRate  = $mHits / $mEff * 100;
                $mProfit = (float)$ms['profit'];
            ?>
            <div class="stat-item">
                <span class="stat-value" style="font-size: 20px;"><?= market_label($ms['market']) ?></span>
                <span class="stat-label">
                    <?= number_format($mRate, 1) ?>% 命中
                    &nbsp;|&nbsp;
                    <span class="<?= $mProfit >= 0 ? 'profit-positive' : 'profit-negative' ?>">
                        <?= $mProfit >= 0 ? '+' : '' ?><?= money_fmt($mProfit) ?>
                    </span>
                </span>
            </div>
            <?php endforeach; ?>
        </div>
        <?php endif; ?>
    </div>
</section>

<!-- ═══════════════════════════════════════════ -->
<!--  今日预测                                    -->
<!-- ═══════════════════════════════════════════ -->
<section class="container mt-4">
    <div class="section-title">
        <h2>📋 今日预测</h2>
        <span class="badge"><?= date('Y-m-d') ?></span>
    </div>

    <!-- Tab 切换栏 -->
    <div class="tab-bar" id="tabBar">
        <button class="tab-btn active" data-tab="all">全部</button>
        <button class="tab-btn" data-tab="live">正在比赛</button>
        <button class="tab-btn" data-tab="finished">已结束</button>
    </div>

    <div class="refresh-bar">
        <span class="refresh-info" id="refreshInfo">上次刷新: --:--:--</span>
        <button class="btn btn-outline btn-sm" id="manualRefreshBtn">🔄 手动刷新</button>
    </div>

    <?php if (empty($predictions)): ?>
    <div class="empty-state">
        <div class="icon">📭</div>
        <p>今日暂无预测数据</p>
        <p style="font-size: 12px; margin-top: 8px;">请运行 <code>php sync.php</code> 同步比赛数据</p>
    </div>
    <?php else: ?>
    <div class="prediction-table" id="todayPredictions">
        <table>
            <thead>
                <tr>
                    <th>联赛</th>
                    <th>开赛时间</th>
                    <th>主队</th>
                    <th>比分</th>
                    <th>客队</th>
                    <th>市场</th>
                    <th>预测</th>
                    <th>置信度</th>
                    <th>赔率</th>
                    <th>状态</th>
                    <th>结果</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ($predictions as $p): ?>
                <tr data-match-status="<?= e($p['match_status'] ?? '') ?>" data-pred-status="<?= e($p['status'] ?? 'pending') ?>">
                    <td class="text-muted" style="font-size: 12px;"><?= e($p['league']) ?></td>
                    <td style="white-space: nowrap;"><?= format_kickoff($p['kickoff']) ?></td>
                    <td class="team-cell"><?= e($p['home_team']) ?></td>
                    <td style="font-weight: 800; color: #7df48b;" data-match-id="<?= e($p['match_id']) ?>"><?= display_score($p['home_score'], $p['away_score']) ?></td>
                    <td class="team-cell"><?= e($p['away_team']) ?></td>
                    <td><?= market_label($p['market']) ?></td>
                    <td><?= direction_badge($p['market'], $p['direction']) ?></td>
                    <td class="confidence-cell"><?= number_format((float)$p['confidence'], 1) ?>%</td>
                    <td class="odds-cell"><?= $p['odds'] ? number_format((float)$p['odds'], 2) : '-' ?></td>
                    <td><?= $p['status'] === 'settled' ? '已结算' : '待结算' ?></td>
                    <td><?= status_badge($p['result_status'] ?? null) ?></td>
                </tr>
                <?php endforeach; ?>
            </tbody>
        </table>
    </div>
    <?php endif; ?>
</section>

<!-- ═══════════════════════════════════════════ -->
<!--  历史回溯统计                               -->
<!-- ═══════════════════════════════════════════ -->
<section class="container mt-4">
    <div class="section-title">
        <h2>📊 历史回溯</h2>
        <span class="badge">最近 50 条</span>
    </div>

    <?php if (empty($settledList)): ?>
    <div class="empty-state">
        <div class="icon">📊</div>
        <p>暂无已结算的预测记录</p>
        <p style="font-size: 12px; margin-top: 8px;">运行 <code>php settle.php</code> 结算已完成比赛</p>
    </div>
    <?php else: ?>
    <div class="prediction-table">
        <table>
            <thead>
                <tr>
                    <th>日期</th>
                    <th>联赛</th>
                    <th>对阵</th>
                    <th>比分</th>
                    <th>市场</th>
                    <th>预测</th>
                    <th>置信度</th>
                    <th>赔率</th>
                    <th>结果</th>
                    <th>盈亏</th>
                    <th>ROI</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ($settledList as $s):
                    $sProfit = (float)($s['profit'] ?? 0);
                    $sRoi    = (float)($s['roi']    ?? 0);
                ?>
                <tr>
                    <td style="white-space: nowrap; font-size: 12px;"><?= format_kickoff($s['kickoff']) ?></td>
                    <td class="text-muted" style="font-size: 12px;"><?= e($s['league']) ?></td>
                    <td class="team-cell"><?= e($s['home_team']) ?> vs <?= e($s['away_team']) ?></td>
                    <td style="font-weight: 800; color: #7df48b;"><?= display_score($s['home_score'], $s['away_score']) ?></td>
                    <td><?= market_label($s['market']) ?></td>
                    <td><?= direction_badge($s['market'], $s['direction']) ?></td>
                    <td class="confidence-cell"><?= number_format((float)$s['confidence'], 1) ?>%</td>
                    <td class="odds-cell"><?= $s['odds'] ? number_format((float)$s['odds'], 2) : '-' ?></td>
                    <td><?= status_badge($s['hit_status']) ?></td>
                    <td class="<?= $sProfit > 0 ? 'profit-positive' : ($sProfit < 0 ? 'profit-negative' : '') ?>">
                        <?= $sProfit >= 0 ? '+' : '' ?><?= money_fmt($sProfit) ?>
                    </td>
                    <td class="<?= $sRoi > 0 ? 'profit-positive' : ($sRoi < 0 ? 'profit-negative' : '') ?>">
                        <?= $sRoi >= 0 ? '+' : '' ?><?= number_format($sRoi, 1) ?>%
                    </td>
                </tr>
                <?php endforeach; ?>
            </tbody>
        </table>
    </div>
    <?php endif; ?>
</section>

<!-- ═══════════════════════════════════════════ -->
<!--  页脚                                        -->
<!-- ═══════════════════════════════════════════ -->
<footer class="footer-note">
    <p>数据来源：football-data.org · The Odds API · Goalserve · 500.com · 足彩网 · njstats.cn</p>
    <p>预测仅供参考，不构成投注建议。API 地址：<?= e(config('api.base_url')) ?></p>
    <p style="margin-top: 8px;">
        <code>php sync.php</code> 同步数据 &nbsp;|&nbsp;
        <code>php settle.php</code> 结算预测
    </p>
</footer>

<!-- ═══════════════════════════════════════════ -->
<!--  JavaScript: Tab 切换 + AJAX 实时刷新        -->
<!-- ═══════════════════════════════════════════ -->
<script>
(function () {
    'use strict';

    var API_BASE = <?= json_encode(config('api.base_url'), JSON_UNESCAPED_SLASHES) ?>;
    var POLL_INTERVAL = 30000;       // 30 秒轮询
    var INITIAL_DELAY = 5000;        // 首轮 5 秒后触发
    var currentTab = 'all';
    var lastEtag = null;
    var pollTimer = null;

    // ── DOM 引用 ─────────────────────────────────
    var tabBtns    = document.querySelectorAll('#tabBar .tab-btn');
    var refreshInfo = document.getElementById('refreshInfo');
    var manualBtn   = document.getElementById('manualRefreshBtn');
    var tableBody   = document.querySelector('#todayPredictions tbody');

    // ── Tab 切换 ─────────────────────────────────
    tabBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            tabBtns.forEach(function (b) { b.classList.remove('active'); });
            this.classList.add('active');
            currentTab = this.getAttribute('data-tab');
            filterRows();
        });
    });

    /**
     * 根据当前 Tab 显示 / 隐藏表格行。
     * - 全部：显示所有行
     * - 正在比赛：match_status 含 LIVE / HT / 进行中
     * - 已结束：match_status 含 FT / 完 / FINISHED，或 pred_status = settled
     */
    function filterRows() {
        if (!tableBody) return;
        var rows = tableBody.querySelectorAll('tr');
        for (var i = 0; i < rows.length; i++) {
            var row         = rows[i];
            var matchStatus = (row.getAttribute('data-match-status') || '').toUpperCase();
            var predStatus  = (row.getAttribute('data-pred-status')  || '').toLowerCase();

            if (currentTab === 'all') {
                row.style.display = '';
            } else if (currentTab === 'live') {
                var isLive = matchStatus.indexOf('LIVE') !== -1
                          || matchStatus.indexOf('HT') !== -1
                          || matchStatus.indexOf('进行中') !== -1;
                row.style.display = isLive ? '' : 'none';
            } else if (currentTab === 'finished') {
                var isFinished = matchStatus.indexOf('FT') !== -1
                              || matchStatus.indexOf('完') !== -1
                              || matchStatus.indexOf('FINISHED') !== -1
                              || predStatus === 'settled';
                row.style.display = isFinished ? '' : 'none';
            }
        }
    }

    // ── 刷新时间显示 ─────────────────────────────
    function updateRefreshTime() {
        var now = new Date();
        var hh  = ('0' + now.getHours()).slice(-2);
        var mm  = ('0' + now.getMinutes()).slice(-2);
        var ss  = ('0' + now.getSeconds()).slice(-2);
        refreshInfo.textContent = '上次刷新: ' + hh + ':' + mm + ':' + ss;
    }

    // ── AJAX 获取进行中比赛并更新 DOM ────────────
    function refreshLiveScores() {
        var headers = {};
        if (lastEtag) {
            headers['If-None-Match'] = lastEtag;
        }

        fetch(API_BASE + '/matches/status?filter=live', { headers: headers })
            .then(function (resp) {
                // 304 Not Modified — 数据未变化
                if (resp.status === 304) {
                    updateRefreshTime();
                    return null;
                }

                var newEtag = resp.headers.get('ETag');
                if (newEtag) lastEtag = newEtag;

                return resp.json();
            })
            .then(function (data) {
                if (data === null) return;          // 304 跳过
                if (!Array.isArray(data)) return;   // 非预期格式

                updateScores(data);
                updateRefreshTime();
            })
            .catch(function (err) {
                console.warn('比分刷新失败:', err);
                updateRefreshTime();                // 即使失败也更新时间
            });
    }

    /**
     * 根据 API 返回的 LiveMatchStatus[] 更新表格中的比分和状态。
     * 比分变化时添加脉冲动画 class。
     */
    function updateScores(liveMatches) {
        // 构建 match_id → 实时数据 的映射
        var scoreMap = {};
        for (var i = 0; i < liveMatches.length; i++) {
            var m = liveMatches[i];
            scoreMap[m.match_id] = {
                home:   m.home_score,
                away:   m.away_score,
                minute: m.minute,
                status: m.status
            };
        }

        // 遍历所有带 data-match-id 的比分单元格
        var scoreCells = document.querySelectorAll('#todayPredictions [data-match-id]');
        for (var j = 0; j < scoreCells.length; j++) {
            var td      = scoreCells[j];
            var matchId = td.getAttribute('data-match-id');
            var info    = scoreMap[matchId];
            if (!info) continue;

            var newScore = info.home + ' : ' + info.away;
            var oldScore = (td.textContent || '').trim();

            // 比分变化 → 脉冲动画
            if (oldScore !== newScore && oldScore !== 'vs') {
                td.classList.add('score-pulse');
                setTimeout(function (cell) {
                    cell.classList.remove('score-pulse');
                }, 600, td);
            }

            td.textContent = newScore;

            // 同步更新行上的 match_status（影响 Tab 筛选）
            var row = td.closest('tr');
            if (row && info.status) {
                row.setAttribute('data-match-status', info.status);
            }
        }

        // 状态更新后重新应用筛选
        filterRows();
    }

    // ── 手动刷新按钮 ─────────────────────────────
    manualBtn.addEventListener('click', function () {
        refreshLiveScores();
    });

    // ── 启动 ─────────────────────────────────────
    updateRefreshTime();
    filterRows();

    // 首轮延迟触发（避免页面刚加载就请求）
    setTimeout(refreshLiveScores, INITIAL_DELAY);

    // 定时轮询
    pollTimer = setInterval(refreshLiveScores, POLL_INTERVAL);

})();
</script>

</body>
</html>