<?php
/**
 * index.php — 足球智能预测首页
 *
 * 功能：
 *   - 🔴 正在比赛：实时卡片网格，AJAX 30s 刷新比分
 *   - 📅 今日赛程：完整比赛列表表格
 *   - 纯 API 驱动，不依赖 SQLite
 */

declare(strict_types=1);

// Render 免费层冷启动可能需 30s+，放宽执行时间限制
set_time_limit(90);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 从 Render API 获取数据
// ---------------------------------------------------------------------------
$apiBase  = config('api.base_url');
$apiError = null;
$liveMatches   = [];
$allMatches    = [];

try {
    // 并行获取进行中比赛 + 全部赛程
    $results = api_get_multi([
        'live' => '/matches/status?filter=live',
        'all'  => '/matches/upcoming',
    ]);
    $liveMatches = $results['live'] ?? [];
    $allMatches  = $results['all']  ?? [];
} catch (RuntimeException $e) {
    $apiError = $e->getMessage();
    $liveMatches = [];
    $allMatches  = [];
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

function is_live(array $m): bool
{
    $status = strtoupper($m['live_status'] ?? $m['status'] ?? '');
    return in_array($status, ['LIVE', 'HT'], true);
}

function is_finished(array $m): bool
{
    $status = strtoupper($m['live_status'] ?? $m['status'] ?? '');
    return in_array($status, ['FT', 'FINISHED'], true);
}

function is_scheduled(array $m): bool
{
    $status = strtoupper($m['live_status'] ?? $m['status'] ?? '');
    return in_array($status, ['SCHEDULED', 'NS'], true);
}

function match_status_label(array $m): string
{
    if (is_live($m)) {
        $min = $m['minute'] ?? 0;
        return $min > 0 ? "{$min}'" : '● LIVE';
    }
    if (is_finished($m)) return '已结束';
    return '未开赛';
}

function match_status_css(array $m): string
{
    if (is_live($m)) return 'live';
    if (is_finished($m)) return 'finished';
    return 'scheduled';
}

function format_kickoff(?string $kickoff): string
{
    if (empty($kickoff)) return '-';
    $ts = strtotime($kickoff);
    if ($ts !== false) return date('H:i', $ts);
    // strtotime 失败时尝试正则提取 HH:MM
    if (preg_match('/(\d{2}:\d{2})/', $kickoff, $m)) return $m[1];
    return '--:--';
}

function format_kickoff_full(?string $kickoff): string
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

function team_name(array $m, string $side): string
{
    $team = $m[$side . '_team'] ?? null;
    if (is_array($team)) return strip_tags($team['name'] ?? '');
    return strip_tags((string) $team);
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
    <title>⚽ 足球智能预测</title>
    <link rel="stylesheet" href="style.css">
</head>
<body>

<!-- ═══════════════════════════════════════════ -->
<!--  顶部导航栏                                  -->
<!-- ═══════════════════════════════════════════ -->
<nav class="navbar">
    <div class="container">
        <a href="/" class="brand">⚽ 足球<span>智能预测</span></a>
        <div class="nav-actions">
            <span class="refresh-info" id="refreshInfo">就绪</span>
            <button class="btn btn-outline btn-sm" id="refreshBtn" title="手动刷新">🔄 刷新</button>
        </div>
    </div>
</nav>

<!-- ═══════════════════════════════════════════ -->
<!--  API 错误提示                               -->
<!-- ═══════════════════════════════════════════ -->
<?php if ($apiError !== null): ?>
<div class="container" style="margin-top:20px;">
    <div class="api-error">
        ⚠️ API 连接失败：<?= e($apiError) ?>
        <br><small>API 地址：<?= e($apiBase) ?> — 请确认服务正在运行</small>
    </div>
</div>
<?php endif; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  🔴 正在比赛                                -->
<!-- ═══════════════════════════════════════════ -->
<section class="container" style="margin-top: 24px;">
    <div class="section-header">
        <h2>🔴 正在比赛 <span class="count-badge"><?= count($liveMatches) ?> 场</span></h2>
    </div>

    <?php if (empty($liveMatches)): ?>
    <div class="empty-state">
        <div class="icon">📭</div>
        <p>当前没有进行中的比赛</p>
        <p style="font-size:12px;margin-top:4px;">数据来源：<?= e($apiBase) ?></p>
    </div>
    <?php else: ?>
    <div class="live-cards" id="liveCards">
        <?php foreach ($liveMatches as $m):
            $mId    = $m['id'] ?? '';
            $league = $m['league'] ?? '';
            $home   = team_name($m, 'home');
            $away   = team_name($m, 'away');
            $hScore = $m['home_score'] ?? 0;
            $aScore = $m['away_score'] ?? 0;
            $minute = $m['minute'] ?? 0;
        ?>
        <a href="detail.php?match_id=<?= urlencode($mId) ?>" class="match-card" data-match-id="<?= e($mId) ?>">
            <div class="card-league">
                <span class="live-dot"></span>
                <?= e($league) ?>
            </div>
            <div class="card-teams">
                <span class="card-team"><?= e($home) ?></span>
                <span class="card-score" data-score-home="<?= (int)$hScore ?>" data-score-away="<?= (int)$aScore ?>">
                    <?= display_score((int)$hScore, (int)$aScore) ?>
                </span>
                <span class="card-team"><?= e($away) ?></span>
            </div>
            <div class="card-minute"><?= $minute > 0 ? "{$minute}'" : '● 进行中' ?></div>
            <div class="card-status">点击查看详情 →</div>
        </a>
        <?php endforeach; ?>
    </div>
    <?php endif; ?>
</section>

<!-- ═══════════════════════════════════════════ -->
<!--  📅 今日赛程                                -->
<!-- ═══════════════════════════════════════════ -->
<section class="container" style="margin-top: 40px;">
    <div class="section-header">
        <h2>📅 今日赛程 <span class="count-badge"><?= count($allMatches) ?> 场</span></h2>
    </div>

    <?php if (empty($allMatches)): ?>
    <div class="empty-state">
        <div class="icon">📅</div>
        <p>暂无赛程数据</p>
        <p style="font-size:12px;margin-top:4px;">数据来源：<?= e($apiBase) ?></p>
    </div>
    <?php else: ?>
    <div style="overflow-x:auto;">
        <table class="schedule-table" id="scheduleTable">
            <thead>
                <tr>
                    <th>时间</th>
                    <th>联赛</th>
                    <th>主队</th>
                    <th></th>
                    <th>客队</th>
                    <th>状态</th>
                    <th>比分</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ($allMatches as $m):
                    $mId     = $m['id'] ?? '';
                    $league  = $m['league'] ?? '';
                    $home    = team_name($m, 'home');
                    $away    = team_name($m, 'away');
                    $hScore  = $m['home_score'] ?? null;
                    $aScore  = $m['away_score'] ?? null;
                    $kickoff = $m['kickoff'] ?? '';
                    $rowClass = is_live($m) ? 'row-live' : '';
                ?>
                <tr class="<?= $rowClass ?>" data-match-id="<?= e($mId) ?>" data-match-status="<?= e($m['live_status'] ?? $m['status'] ?? '') ?>">
                    <td style="white-space:nowrap;font-size:13px;color:#94a3b8;"><?= format_kickoff($kickoff) ?></td>
                    <td style="font-size:12px;color:#64748b;"><?= e($league) ?></td>
                    <td class="team-cell"><?= e($home) ?></td>
                    <td class="vs-cell">vs</td>
                    <td class="team-cell"><?= e($away) ?></td>
                    <td>
                        <span class="status-badge <?= match_status_css($m) ?>">
                            <?= match_status_label($m) ?>
                        </span>
                    </td>
                    <td style="font-weight:800;color:#22c55e;" class="schedule-score-cell">
                        <?= display_score(
                            $hScore !== null ? (int)$hScore : null,
                            $aScore !== null ? (int)$aScore : null
                        ) ?>
                    </td>
                    <td>
                        <a href="detail.php?match_id=<?= urlencode($mId) ?>" class="btn btn-outline btn-sm">详情 →</a>
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
<footer class="site-footer">
    <p>数据来源：football-data.org · The Odds API · Goalserve · 500.com · 足彩网 · njstats.cn · 捷报比分</p>
    <p>预测仅供参考，不构成投注建议。API 地址：<?= e($apiBase) ?></p>
</footer>

<!-- ═══════════════════════════════════════════ -->
<!--  JavaScript: AJAX 实时比分刷新              -->
<!-- ═══════════════════════════════════════════ -->
<script>
(function () {
    'use strict';

    var API_BASE = <?= json_encode($apiBase, JSON_UNESCAPED_SLASHES) ?>;
    var POLL_INTERVAL = 30000;       // 30 秒
    var INITIAL_DELAY = 5000;        // 首轮 5 秒后触发
    var pollTimer = null;

    // ── DOM 引用 ─────────────────────────────────
    var refreshInfo = document.getElementById('refreshInfo');
    var refreshBtn  = document.getElementById('refreshBtn');
    var liveCards   = document.getElementById('liveCards');

    // ── 刷新时间显示 ─────────────────────────────
    function updateRefreshTime() {
        var now = new Date();
        var hh  = ('0' + now.getHours()).slice(-2);
        var mm  = ('0' + now.getMinutes()).slice(-2);
        var ss  = ('0' + now.getSeconds()).slice(-2);
        if (refreshInfo) {
            refreshInfo.textContent = '更新于 ' + hh + ':' + mm + ':' + ss;
        }
    }

    // ── AJAX 获取进行中比赛并更新 DOM ────────────
    function refreshLiveScores() {
        fetch(API_BASE + '/matches/status?filter=live')
            .then(function (resp) {
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                return resp.json();
            })
            .then(function (data) {
                if (!Array.isArray(data)) return;
                updateLiveCards(data);
                updateScheduleRows(data);
                updateRefreshTime();
            })
            .catch(function (err) {
                console.warn('比分刷新失败:', err);
                updateRefreshTime();
            });
    }

    /**
     * 更新实时比赛卡片中的比分。
     */
    function updateLiveCards(liveData) {
        if (!liveCards) return;

        // 构建 match_id → 实时数据 映射
        var map = {};
        for (var i = 0; i < liveData.length; i++) {
            var m = liveData[i];
            map[m.id] = {
                home:   m.home_score,
                away:   m.away_score,
                minute: m.minute,
                status: m.status || m.live_status
            };
        }

        // 更新现有卡片
        var cards = liveCards.querySelectorAll('.match-card');
        for (var j = 0; j < cards.length; j++) {
            var card    = cards[j];
            var matchId = card.getAttribute('data-match-id');
            var info    = map[matchId];
            if (!info) continue;

            var scoreEl = card.querySelector('.card-score');
            var minuteEl = card.querySelector('.card-minute');

            if (scoreEl) {
                var newScore = info.home + ' : ' + info.away;
                var oldScore = (scoreEl.textContent || '').trim();

                if (oldScore !== newScore && oldScore !== 'vs') {
                    scoreEl.classList.add('score-pulse');
                    setTimeout(function (el) {
                        el.classList.remove('score-pulse');
                    }, 600, scoreEl);
                }
                scoreEl.textContent = newScore;
                scoreEl.setAttribute('data-score-home', info.home);
                scoreEl.setAttribute('data-score-away', info.away);
            }

            if (minuteEl) {
                minuteEl.textContent = info.minute > 0 ? info.minute + "'" : '● 进行中';
            }
        }
    }

    /**
     * 更新赛程表格中的比分和状态。
     */
    function updateScheduleRows(liveData) {
        var table = document.getElementById('scheduleTable');
        if (!table) return;

        var map = {};
        for (var i = 0; i < liveData.length; i++) {
            var m = liveData[i];
            map[m.id] = {
                home:   m.home_score,
                away:   m.away_score,
                minute: m.minute,
                status: m.status || m.live_status
            };
        }

        var rows = table.querySelectorAll('tbody tr[data-match-id]');
        for (var j = 0; j < rows.length; j++) {
            var row     = rows[j];
            var matchId = row.getAttribute('data-match-id');
            var info    = map[matchId];
            if (!info) continue;

            // 更新比分
            var scoreCell = row.querySelector('.schedule-score-cell');
            if (scoreCell) {
                scoreCell.textContent = info.home + ' : ' + info.away;
            }

            // 更新状态标签
            var statusBadge = row.querySelector('.status-badge');
            if (statusBadge) {
                var isLive = info.status === 'LIVE' || info.status === 'HT';
                var isFinished = info.status === 'FT' || info.status === 'FINISHED';

                if (isLive) {
                    statusBadge.className = 'status-badge live';
                    statusBadge.textContent = info.minute > 0 ? info.minute + "'" : '● LIVE';
                    row.classList.add('row-live');
                } else if (isFinished) {
                    statusBadge.className = 'status-badge finished';
                    statusBadge.textContent = '已结束';
                    row.classList.remove('row-live');
                }
            }
        }
    }

    // ── 手动刷新 ─────────────────────────────────
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function () {
            refreshLiveScores();
        });
    }

    // ── 启动 ─────────────────────────────────────
    updateRefreshTime();

    // 首轮延迟触发
    setTimeout(refreshLiveScores, INITIAL_DELAY);

    // 定时轮询
    pollTimer = setInterval(refreshLiveScores, POLL_INTERVAL);

    // 页面隐藏时暂停轮询
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        } else {
            refreshLiveScores();
            if (!pollTimer) {
                pollTimer = setInterval(refreshLiveScores, POLL_INTERVAL);
            }
        }
    });

})();
</script>

</body>
</html>