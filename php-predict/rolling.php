<?php
/**
 * rolling.php — 滚球预测板块
 *
 * 功能：
 *   - 展示所有进行中且 minute ≥ 10 的比赛
 *   - 每场比赛显示滚球预测卡片（WDL 调整、大小球调整、下一进球预测）
 *   - AJAX 30s 自动刷新实时比分和预测
 *   - 点击比赛可跳转详情页
 */

declare(strict_types=1);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
init_db();
$db = db();

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

function market_label(string $market): string
{
    return match ($market) {
        '1x2'            => '胜平负',
        'over_under'     => '大小球',
        'asian_handicap' => '亚盘',
        default          => $market,
    };
}

function direction_badge(string $market, string $direction): string
{
    return match ($market) {
        '1x2' => match (true) {
            str_contains($direction, '主胜') || str_contains($direction, 'home') => '<span class="badge badge-home">主胜</span>',
            str_contains($direction, '平')   || str_contains($direction, 'draw') => '<span class="badge badge-draw">平局</span>',
            str_contains($direction, '客胜') || str_contains($direction, 'away') => '<span class="badge badge-away">客胜</span>',
            default => '<span class="badge">' . e($direction) . '</span>',
        },
        'over_under' => match (true) {
            str_contains($direction, '大') || str_contains($direction, 'over')  => '<span class="badge badge-over">大球</span>',
            str_contains($direction, '小') || str_contains($direction, 'under') => '<span class="badge badge-under">小球</span>',
            default => '<span class="badge">' . e($direction) . '</span>',
        },
        'asian_handicap' => match (true) {
            str_contains($direction, '主') || str_contains($direction, 'home') => '<span class="badge badge-home">主队</span>',
            str_contains($direction, '客') || str_contains($direction, 'away') => '<span class="badge badge-away">客队</span>',
            default => '<span class="badge">' . e($direction) . '</span>',
        },
        default => '<span class="badge">' . e($direction) . '</span>',
    };
}

function confidence_level_class(string $level): string
{
    return match (strtoupper($level)) {
        'A' => 'confidence-a',
        'B' => 'confidence-b',
        'C' => 'confidence-c',
        'D' => 'confidence-d',
        default => 'confidence-d',
    };
}

function confidence_level_text(string $level): string
{
    return match (strtoupper($level)) {
        'A' => '极高信心',
        'B' => '高信心',
        'C' => '中等信心',
        'D' => '低信心',
        default => '未知',
    };
}

function format_kickoff(?string $kickoff): string
{
    if ($kickoff === null || $kickoff === '') {
        return '-';
    }
    $ts = strtotime($kickoff);
    if ($ts === false) {
        return e($kickoff);
    }
    return date('m-d H:i', $ts);
}

function display_score(?int $home, ?int $away): string
{
    if ($home === null || $away === null) {
        return 'vs';
    }
    return "{$home} : {$away}";
}

function match_status_text(string $status): string
{
    return match (strtoupper($status)) {
        'LIVE', '1H', '2H' => '● 进行中',
        'HT'               => '⏸ 中场',
        'FT', 'FINISHED'   => '✓ 已结束',
        default            => e($status),
    };
}

// ---------------------------------------------------------------------------
// 查询进行中比赛（minute >= 10）
// ---------------------------------------------------------------------------
$result = $db->query("
    SELECT match_id, home_team, away_team, league, kickoff, status,
           home_score, away_score, COALESCE(minute, 0) AS minute
    FROM matches
    WHERE status LIKE '%LIVE%' OR status LIKE '%HT%'
    ORDER BY COALESCE(minute, 0) DESC
");

$liveMatches = [];
while ($row = $result->fetchArray(SQLITE3_ASSOC)) {
    $minute = (int) ($row['minute'] ?? 0);
    if ($minute >= 10) {
        $liveMatches[] = $row;
    }
}

// ---------------------------------------------------------------------------
// 查询滚球预测数据
// ---------------------------------------------------------------------------
$rollingPredictions = [];
if (!empty($liveMatches)) {
    $stmt = $db->prepare('
        SELECT * FROM rolling_predictions
        WHERE match_id = :match_id
        ORDER BY created_at DESC
    ');
    foreach ($liveMatches as $m) {
        $stmt->bindValue(':match_id', $m['match_id'], SQLITE3_TEXT);
        $res = $stmt->execute();
        $preds = [];
        while ($p = $res->fetchArray(SQLITE3_ASSOC)) {
            $preds[] = $p;
        }
        $stmt->reset();
        $rollingPredictions[$m['match_id']] = $preds;
    }
}

// ---------------------------------------------------------------------------
// 按预测类型分组
// ---------------------------------------------------------------------------
function group_predictions(array $preds): array
{
    $groups = [];
    foreach ($preds as $p) {
        $ptype = $p['prediction_type'] ?? $p['direction'] ?? 'other';
        $groups[$ptype][] = $p;
    }
    return $groups;
}

function prediction_type_label(string $type): string
{
    return match ($type) {
        'wdl_adjusted'      => '胜平负调整',
        'over_under_adjusted' => '大小球调整',
        'next_goal'         => '下一进球',
        'asian_adjusted'    => '亚盘调整',
        default             => $type,
    };
}

function prediction_type_icon(string $type): string
{
    return match ($type) {
        'wdl_adjusted'        => '⚽',
        'over_under_adjusted' => '📊',
        'next_goal'           => '🎯',
        'asian_adjusted'      => '🏟️',
        default               => '📌',
    };
}

?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>滚球预测 — 足球智能预测</title>
    <link rel="stylesheet" href="style.css">
    <style>
        /* 滚球页面特有样式 */
        .rolling-page {
            max-width: 960px;
            margin: 0 auto;
            padding: 24px 16px;
        }

        .rolling-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 24px;
        }

        .rolling-header h1 {
            font-size: clamp(20px, 4vw, 28px);
            font-weight: 950;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .rolling-header h1 .live-dot {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #ff4444;
            animation: live-dot-pulse 1.2s ease-in-out infinite;
        }

        .rolling-meta {
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }

        .rolling-count {
            font-size: 14px;
            font-weight: 800;
            color: var(--green);
            background: rgba(125, 244, 139, 0.10);
            padding: 6px 14px;
            border-radius: 999px;
        }

        .rolling-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 20px;
        }

        @media (min-width: 700px) {
            .rolling-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .match-rolling-card {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border);
            border-radius: 18px;
            overflow: hidden;
            backdrop-filter: blur(10px);
            transition: border-color 0.2s, transform 0.2s;
        }

        .match-rolling-card:hover {
            border-color: rgba(125, 244, 139, 0.35);
            transform: translateY(-2px);
        }

        .match-rolling-card .card-top-bar {
            height: 3px;
            background: linear-gradient(90deg, var(--green), #33b8ff);
            opacity: 0.7;
        }

        .match-rolling-card .card-header {
            padding: 16px 20px 12px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 8px;
        }

        .match-rolling-card .card-league {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
        }

        .match-rolling-card .card-minute {
            font-size: 13px;
            font-weight: 800;
            color: var(--green);
            white-space: nowrap;
        }

        .match-rolling-card .card-teams {
            padding: 0 20px 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            flex-wrap: wrap;
        }

        .match-rolling-card .card-team {
            font-size: 16px;
            font-weight: 900;
            color: #fff;
        }

        .match-rolling-card .card-score {
            font-size: 24px;
            font-weight: 950;
            color: var(--green);
            min-width: 60px;
            text-align: center;
        }

        .match-rolling-card .card-predictions {
            padding: 0 20px 16px;
        }

        .pred-group {
            margin-bottom: 12px;
        }

        .pred-group:last-child {
            margin-bottom: 0;
        }

        .pred-group-label {
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .pred-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 10px;
            margin-bottom: 6px;
        }

        .pred-item:last-child {
            margin-bottom: 0;
        }

        .pred-item .pred-direction {
            font-size: 14px;
            font-weight: 800;
            color: #fff;
        }

        .pred-item .pred-meta {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .pred-item .pred-prob {
            font-size: 13px;
            font-weight: 800;
            color: var(--green);
        }

        .pred-item .pred-rationale {
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 4px;
            line-height: 1.4;
        }

        .card-footer {
            padding: 10px 20px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            text-align: right;
        }

        .card-footer .detail-link {
            font-size: 12px;
            font-weight: 700;
            color: var(--green);
            text-decoration: none;
            transition: opacity 0.2s;
        }

        .card-footer .detail-link:hover {
            opacity: 0.8;
            text-decoration: underline;
        }

        .empty-rolling {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-muted);
        }

        .empty-rolling .icon {
            font-size: 48px;
            margin-bottom: 16px;
        }

        .empty-rolling p {
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 8px;
        }

        .empty-rolling .hint {
            font-size: 12px;
            opacity: 0.7;
        }
    </style>
</head>
<body>

<!-- ═══════════════════════════════════════════ -->
<!--  导航栏                                      -->
<!-- ═══════════════════════════════════════════ -->
<nav class="navbar">
    <div class="nav-inner">
        <a href="index.php" class="nav-brand">⚽ 足球智能预测</a>
        <div class="nav-links">
            <a href="index.php">首页</a>
            <a href="rolling.php" class="active">滚球预测</a>
        </div>
    </div>
</nav>

<!-- ═══════════════════════════════════════════ -->
<!--  主内容区                                    -->
<!-- ═══════════════════════════════════════════ -->
<main class="rolling-page">
    <div class="rolling-header">
        <h1>
            <span class="live-dot"></span>
            滚球预测
        </h1>
        <div class="rolling-meta">
            <span class="rolling-count" id="matchCount">
                <?= count($liveMatches) ?> 场进行中
            </span>
            <span class="refresh-status" id="refreshStatus">
                <span id="refreshTime">上次刷新: --:--:--</span>
                <button class="btn btn-outline btn-sm" id="manualRefreshBtn" style="margin-left: 8px;">🔄 刷新</button>
            </span>
        </div>
    </div>

    <?php if (empty($liveMatches)): ?>
    <div class="empty-rolling">
        <div class="icon">📭</div>
        <p>当前没有符合条件的滚球比赛</p>
        <p class="hint">需要比赛进行到 10 分钟以上才会显示滚球预测</p>
        <p class="hint">请运行 <code>php sync.php --rolling</code> 同步滚球预测数据</p>
    </div>
    <?php else: ?>
    <div class="rolling-grid" id="rollingGrid">
        <?php foreach ($liveMatches as $m):
            $matchId  = $m['match_id'];
            $homeTeam = $m['home_team'];
            $awayTeam = $m['away_team'];
            $hScore   = (int) ($m['home_score'] ?? 0);
            $aScore   = (int) ($m['away_score'] ?? 0);
            $minute   = (int) ($m['minute'] ?? 0);
            $preds    = $rollingPredictions[$matchId] ?? [];
            $groups   = group_predictions($preds);
        ?>
        <div class="match-rolling-card" data-match-id="<?= e($matchId) ?>">
            <div class="card-top-bar"></div>
            <div class="card-header">
                <span class="card-league"><?= e($m['league']) ?></span>
                <span class="card-minute" data-minute="<?= $minute ?>"><?= $minute ?>'</span>
            </div>
            <div class="card-teams">
                <span class="card-team"><?= e($homeTeam) ?></span>
                <span class="card-score" data-score-cell="<?= e($matchId) ?>"><?= $hScore ?> : <?= $aScore ?></span>
                <span class="card-team"><?= e($awayTeam) ?></span>
            </div>
            <div class="card-predictions">
                <?php if (empty($groups)): ?>
                <div style="padding: 12px; text-align: center; font-size: 12px; color: var(--text-muted);">
                    ⚠ 暂无滚球预测数据，请运行 <code>php sync.php --rolling</code>
                </div>
                <?php else: ?>
                    <?php foreach ($groups as $ptype => $items): ?>
                    <div class="pred-group">
                        <div class="pred-group-label">
                            <?= prediction_type_icon($ptype) ?>
                            <?= prediction_type_label($ptype) ?>
                        </div>
                        <?php foreach ($items as $item):
                            $prob  = (float) ($item['probability'] ?? 0);
                            $clevel = $item['confidence_level'] ?? 'C';
                        ?>
                        <div class="pred-item">
                            <div>
                                <div class="pred-direction"><?= direction_badge($item['prediction_type'] ?? '1x2', $item['direction'] ?? '') ?></div>
                                <?php if (!empty($item['rationale'])): ?>
                                <div class="pred-rationale"><?= e($item['rationale']) ?></div>
                                <?php endif; ?>
                            </div>
                            <div class="pred-meta">
                                <span class="pred-prob"><?= number_format($prob * 100, 1) ?>%</span>
                                <span class="<?= confidence_level_class($clevel) ?>"><?= e($clevel) ?></span>
                            </div>
                        </div>
                        <?php endforeach; ?>
                    </div>
                    <?php endforeach; ?>
                <?php endif; ?>
            </div>
            <div class="card-footer">
                <a href="detail.php?match_id=<?= urlencode($matchId) ?>" class="detail-link">查看详情 →</a>
            </div>
        </div>
        <?php endforeach; ?>
    </div>
    <?php endif; ?>
</main>

<!-- ═══════════════════════════════════════════ -->
<!--  页脚                                        -->
<!-- ═══════════════════════════════════════════ -->
<footer class="footer-note">
    <p>滚球预测基于赛前赔率 + 当前比分静态推导，标注"模拟"，仅供参考</p>
    <p>数据来源：football-data.org · The Odds API · Goalserve · 500.com · 足彩网 · njstats.cn</p>
</footer>

<!-- ═══════════════════════════════════════════ -->
<!--  JavaScript: AJAX 实时刷新                   -->
<!-- ═══════════════════════════════════════════ -->
<script>
(function () {
    'use strict';

    var API_BASE = <?= json_encode(config('api.base_url'), JSON_UNESCAPED_SLASHES) ?>;
    var POLL_INTERVAL = 30000;
    var INITIAL_DELAY = 5000;
    var pollTimer = null;

    var refreshTime  = document.getElementById('refreshTime');
    var manualBtn    = document.getElementById('manualRefreshBtn');
    var rollingGrid  = document.getElementById('rollingGrid');
    var matchCountEl = document.getElementById('matchCount');

    // ── 刷新时间 ─────────────────────────────────
    function updateRefreshTime() {
        if (!refreshTime) return;
        var now = new Date();
        var hh  = ('0' + now.getHours()).slice(-2);
        var mm  = ('0' + now.getMinutes()).slice(-2);
        var ss  = ('0' + now.getSeconds()).slice(-2);
        refreshTime.textContent = '上次刷新: ' + hh + ':' + mm + ':' + ss;
    }

    // ── AJAX 刷新实时比分 ────────────────────────
    function refreshLiveData() {
        fetch(API_BASE + '/matches/live')
            .then(function (resp) {
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                return resp.json();
            })
            .then(function (data) {
                if (!Array.isArray(data)) return;
                updateCards(data);
                updateRefreshTime();
            })
            .catch(function (err) {
                console.warn('滚球数据刷新失败:', err);
                updateRefreshTime();
            });
    }

    /**
     * 根据 API 返回的 LiveMatchStatus[] 更新卡片中的比分和分钟。
     */
    function updateCards(liveMatches) {
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

        if (!rollingGrid) return;

        var cards = rollingGrid.querySelectorAll('.match-rolling-card');
        var liveCount = 0;

        for (var j = 0; j < cards.length; j++) {
            var card    = cards[j];
            var matchId = card.getAttribute('data-match-id');
            var info    = scoreMap[matchId];

            if (!info) {
                // 比赛可能已结束，隐藏卡片
                card.style.display = 'none';
                continue;
            }

            liveCount++;
            card.style.display = '';

            // 更新分钟
            var minuteEl = card.querySelector('[data-minute]');
            if (minuteEl) {
                var oldMinute = parseInt(minuteEl.getAttribute('data-minute'), 10) || 0;
                if (info.minute !== oldMinute) {
                    minuteEl.setAttribute('data-minute', info.minute);
                    minuteEl.textContent = info.minute + "'";
                }
            }

            // 更新比分
            var scoreCell = card.querySelector('[data-score-cell]');
            if (scoreCell) {
                var newScore = info.home + ' : ' + info.away;
                var oldScore = (scoreCell.textContent || '').trim();
                if (oldScore !== newScore && oldScore !== 'vs') {
                    scoreCell.classList.add('score-updated');
                    setTimeout(function (el) {
                        el.classList.remove('score-updated');
                    }, 600, scoreCell);
                }
                scoreCell.textContent = newScore;
            }
        }

        // 更新计数
        if (matchCountEl) {
            matchCountEl.textContent = liveCount + ' 场进行中';
        }
    }

    // ── 手动刷新 ─────────────────────────────────
    if (manualBtn) {
        manualBtn.addEventListener('click', function () {
            refreshLiveData();
        });
    }

    // ── 启动 ─────────────────────────────────────
    updateRefreshTime();

    setTimeout(refreshLiveData, INITIAL_DELAY);
    pollTimer = setInterval(refreshLiveData, POLL_INTERVAL);

})();
</script>

</body>
</html>