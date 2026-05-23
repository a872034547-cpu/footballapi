<?php
/**
 * 比赛详情页 — 预测结果置顶 + 溯源证据 + 详细数据
 *
 * 纯 API 驱动，使用 api_get_multi() 并行请求：
 *   - /match/{id}/analysis   → 预测 + 溯源
 *   - /match/{id}/kelly      → 凯利指数
 *   - /match/{id}/rolling    → 滚球预测
 */

require_once __DIR__ . '/functions.php';

// Render 免费层冷启动可能需 30s+，放宽执行时间限制
set_time_limit(90);

$matchId = $_GET['match_id'] ?? '';
if ($matchId === '') {
    http_response_code(400);
    echo '<!DOCTYPE html><html><head><meta charset="utf-8"><title>缺少参数</title>';
    echo '<link rel="stylesheet" href="/style.css"></head><body>';
    echo '<div class="container"><div class="api-error">缺少比赛 ID 参数。请从首页选择一场比赛。</div></div>';
    echo '</body></html>';
    exit;
}

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

function direction_badge(string $market, string $label): string
{
    return match ($market) {
        '1x2' => match (true) {
            str_contains($label, 'home')  => '主胜',
            str_contains($label, 'draw')  => '平局',
            str_contains($label, 'away')  => '客胜',
            default                       => $label,
        },
        'over_under' => match (true) {
            str_contains($label, 'over')  => '大球',
            str_contains($label, 'under') => '小球',
            default                       => $label,
        },
        'asian_handicap' => match (true) {
            str_contains($label, 'home')  => '主队',
            str_contains($label, 'away')  => '客队',
            default                       => $label,
        },
        default => $label,
    };
}

function confidence_class(string $level): string
{
    return match (strtoupper($level)) {
        'A' => 'confidence-a',
        'B' => 'confidence-b',
        'C' => 'confidence-c',
        'D' => 'confidence-d',
        default => 'confidence-c',
    };
}

function confidence_text(string $level): string
{
    return match (strtoupper($level)) {
        'A' => '高信心',
        'B' => '较高信心',
        'C' => '中等信心',
        'D' => '低信心',
        default => '未知',
    };
}

function pattern_type_label(string $type): string
{
    return match ($type) {
        'handicap_derivation' => '盘口推导',
        'odds_implied'        => '赔率隐含概率',
        'asian_water'         => '亚盘水位分析',
        'totals'              => '进球预期分析',
        default               => $type,
    };
}

function rolling_type_label(string $type): string
{
    return match ($type) {
        'win_draw_lose'   => '胜平负',
        'over_under'      => '大小球',
        'asian_handicap'  => '亚盘',
        'next_goal'       => '下一进球',
        default           => $type,
    };
}

function rolling_type_icon(string $type): string
{
    return match ($type) {
        'win_draw_lose'   => '⚽',
        'over_under'      => '📊',
        'asian_handicap'  => '🏆',
        'next_goal'       => '🎯',
        default           => '📌',
    };
}

function format_kickoff(?string $kickoff): string
{
    if (empty($kickoff)) return '--';
    try {
        $dt = new DateTime($kickoff);
        return $dt->format('m/d H:i');
    } catch (\Throwable) {
        return $kickoff;
    }
}

function display_score(?int $home, ?int $away): string
{
    if ($home === null || $away === null) return 'vs';
    return "{$home} - {$away}";
}

function is_live(array $m): bool
{
    $s = strtoupper($m['status'] ?? '');
    return in_array($s, ['LIVE', 'HT', '1H', '2H', 'FIRST_HALF', 'SECOND_HALF', 'HALFTIME'], true);
}

function is_finished(array $m): bool
{
    $s = strtoupper($m['status'] ?? '');
    return in_array($s, ['FT', 'FINISHED', 'AET', 'PEN', 'FULL_TIME'], true);
}

function status_text(array $m): string
{
    $s = strtoupper($m['status'] ?? '');
    return match ($s) {
        'LIVE', '1H', 'FIRST_HALF'  => '● 进行中',
        'HT', 'HALFTIME'            => '⏸ 中场',
        '2H', 'SECOND_HALF'         => '● 下半场',
        'FT', 'FINISHED', 'FULL_TIME' => '✓ 已结束',
        'AET'                       => '✓ 加时',
        'PEN'                       => '✓ 点球',
        default                     => '未开始',
    };
}

function kelly_color(?float $v): string
{
    if ($v === null) return 'kelly-neutral';
    if ($v > 1.05) return 'kelly-high';
    if ($v > 0.95) return 'kelly-good';
    return 'kelly-low';
}

function kelly_label(?float $v): string
{
    if ($v === null) return '--';
    if ($v > 1.05) return '✓ 有利';
    if ($v > 0.95) return '≈ 持平';
    return '✗ 不利';
}

// ---------------------------------------------------------------------------
// 数据获取
// ---------------------------------------------------------------------------

$analysis = null;
$kelly    = null;
$rolling  = null;
$error    = null;

try {
    $results = api_get_multi([
        'analysis' => "match/{$matchId}/analysis",
        'kelly'    => "match/{$matchId}/kelly",
        'rolling'  => "match/{$matchId}/rolling",
    ]);
    $analysis = $results['analysis'];
    $kelly    = $results['kelly'];
    $rolling  = $results['rolling'];
} catch (\Throwable $e) {
    $error = $e->getMessage();
}

// 从 analysis 中提取 match 信息
$match = $analysis['match'] ?? [];
$live  = is_live($match);
$finished = is_finished($match);

// 预测数据
$confidenceScore  = $analysis['confidence_score'] ?? 0;
$confidenceLevel  = $analysis['overall_confidence_level'] ?? 'C';
$benefitFactors   = $analysis['benefit_factors'] ?? [];
$riskFactors      = $analysis['risk_factors'] ?? [];
$preMatchNotes    = $analysis['pre_match_notes'] ?? [];
$predictedScores  = $analysis['predicted_scores'] ?? [];
$wdl              = $analysis['win_draw_lose'] ?? [];
$ou               = $analysis['over_under'] ?? [];
$ah               = $analysis['asian_handicap'] ?? [];
$providerEvidence = $analysis['provider_evidence'] ?? [];
$patternMatches   = $analysis['pattern_matches'] ?? [];
$rollingPreds     = $analysis['rolling_predictions'] ?? [];

// 凯利数据
$kellyHome  = $kelly['home_kelly'] ?? null;
$kellyDraw  = $kelly['draw_kelly'] ?? null;
$kellyAway  = $kelly['away_kelly'] ?? null;
$kellyOver  = $kelly['over_kelly'] ?? null;
$kellyUnder = $kelly['under_kelly'] ?? null;
$kellyAhH   = $kelly['asian_home_kelly'] ?? null;
$kellyAhA   = $kelly['asian_away_kelly'] ?? null;
$kellyCount = $kelly['bookmaker_count'] ?? 0;
$kellyDetails = $kelly['bookmaker_details'] ?? [];

// 推荐方向
$recommendation = '';
$recConfidence  = '';
if (!empty($wdl)) {
    $top = $wdl[0];
    $recommendation = direction_badge('1x2', $top['label'] ?? '');
    $recConfidence   = round(($top['probability'] ?? 0) * 100);
}
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= e($match['home_team']['name'] ?? $match['home_team'] ?? '?') ?> vs <?= e($match['away_team']['name'] ?? $match['away_team'] ?? '?') ?> — 比赛详情</title>
    <link rel="stylesheet" href="/style.css">
</head>
<body>

<nav class="navbar">
    <div class="container">
        <a href="/" class="navbar-brand">⚽ 足球智能预测</a>
        <div class="navbar-links">
            <a href="/">← 返回首页</a>
        </div>
    </div>
</nav>

<main class="container">

<?php if ($error !== null): ?>
    <div class="api-error">
        <strong>数据加载失败</strong>
        <p><?= e($error) ?></p>
        <a href="/" class="btn btn-outline">返回首页</a>
    </div>
<?php else: ?>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- 比赛 Hero 卡片                                                      -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <div class="match-hero <?= $live ? 'match-hero-live' : ($finished ? 'match-hero-finished' : '') ?>">
        <div class="match-hero-league"><?= e($match['league'] ?? '未知联赛') ?></div>
        <div class="match-hero-teams">
            <div class="match-hero-team match-hero-home">
                <span class="team-name"><?= e($match['home_team']['name'] ?? $match['home_team'] ?? '?') ?></span>
            </div>
            <div class="match-hero-score">
                <span class="score-display"><?= display_score($match['home_score'] ?? null, $match['away_score'] ?? null) ?></span>
                <?php if ($live && isset($match['minute'])): ?>
                    <span class="live-minute"><?= (int)$match['minute'] ?>′</span>
                <?php endif; ?>
                <span class="match-status-badge"><?= status_text($match) ?></span>
            </div>
            <div class="match-hero-team match-hero-away">
                <span class="team-name"><?= e($match['away_team']['name'] ?? $match['away_team'] ?? '?') ?></span>
            </div>
        </div>
        <div class="match-hero-meta">
            <span>开赛：<?= format_kickoff($match['kickoff'] ?? null) ?></span>
            <?php if (!empty($match['id'])): ?>
                <span class="match-id">ID: <?= e($match['id']) ?></span>
            <?php endif; ?>
        </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- 🎯 预测结果 (置顶)                                                   -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="section">
        <h2 class="section-title">🎯 综合预测</h2>

        <!-- 信心 + 推荐 -->
        <div class="prediction-summary">
            <div class="confidence-badge <?= confidence_class($confidenceLevel) ?>">
                <span class="confidence-letter"><?= e(strtoupper($confidenceLevel)) ?></span>
                <span class="confidence-label"><?= confidence_text($confidenceLevel) ?></span>
            </div>
            <div class="confidence-score-bar">
                <div class="score-bar-fill" style="width: <?= min(100, max(0, $confidenceScore)) ?>%"></div>
            </div>
            <div class="confidence-score-text">综合评分 <?= round($confidenceScore, 1) ?>/100</div>
            <?php if ($recommendation !== ''): ?>
                <div class="recommendation-badge">
                    推荐：<strong><?= e($recommendation) ?></strong>
                    <?php if ($recConfidence > 0): ?>
                        <span class="rec-prob">(<?= $recConfidence ?>%)</span>
                    <?php endif; ?>
                </div>
            <?php endif; ?>
        </div>

        <!-- 三大市场预测 -->
        <div class="market-predictions-grid">
            <?php
            $markets = [
                ['title' => '胜平负', 'key' => '1x2', 'items' => $wdl],
                ['title' => '大小球', 'key' => 'over_under', 'items' => $ou],
                ['title' => '亚盘',   'key' => 'asian_handicap', 'items' => $ah],
            ];
            foreach ($markets as $mkt):
            ?>
            <div class="market-card">
                <h3 class="market-card-title"><?= $mkt['title'] ?></h3>
                <?php if (empty($mkt['items'])): ?>
                    <div class="text-muted">暂无数据</div>
                <?php else: ?>
                    <?php foreach ($mkt['items'] as $pred): ?>
                        <?php $prob = round(($pred['probability'] ?? 0) * 100); ?>
                        <div class="market-prediction-row">
                            <span class="mp-label"><?= e(direction_badge($mkt['key'], $pred['label'] ?? '')) ?></span>
                            <div class="mp-bar-track">
                                <div class="mp-bar-fill" style="width: <?= $prob ?>%"></div>
                            </div>
                            <span class="mp-prob"><?= $prob ?>%</span>
                        </div>
                        <?php if (!empty($pred['rationale'])): ?>
                            <div class="mp-rationale"><?= e($pred['rationale']) ?></div>
                        <?php endif; ?>
                    <?php endforeach; ?>
                <?php endif; ?>
            </div>
            <?php endforeach; ?>
        </div>

        <!-- 比分预测 -->
        <?php if (!empty($predictedScores)): ?>
        <div class="score-predictions">
            <h3 class="subsection-title">📊 比分预测</h3>
            <div class="score-prediction-list">
                <?php foreach (array_slice($predictedScores, 0, 5) as $sp): ?>
                    <div class="score-prediction-item">
                        <span class="sp-score"><?= e($sp['score'] ?? '?') ?></span>
                        <div class="sp-bar-track">
                            <div class="sp-bar-fill" style="width: <?= round(($sp['probability'] ?? 0) * 100) ?>%"></div>
                        </div>
                        <span class="sp-prob"><?= round(($sp['probability'] ?? 0) * 100) ?>%</span>
                    </div>
                <?php endforeach; ?>
            </div>
        </div>
        <?php endif; ?>
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- 📋 预测溯源证据                                                      -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="section">
        <h2 class="section-title">📋 预测溯源</h2>

        <!-- 有利因素 / 风险因素 -->
        <div class="evidence-grid">
            <?php if (!empty($benefitFactors)): ?>
            <div class="evidence-card evidence-benefit">
                <h3 class="evidence-title benefit-title">✅ 有利因素</h3>
                <ul class="evidence-list">
                    <?php foreach ($benefitFactors as $f): ?>
                        <li><?= e($f) ?></li>
                    <?php endforeach; ?>
                </ul>
            </div>
            <?php endif; ?>

            <?php if (!empty($riskFactors)): ?>
            <div class="evidence-card evidence-risk">
                <h3 class="evidence-title risk-title">⚠️ 风险因素</h3>
                <ul class="evidence-list">
                    <?php foreach ($riskFactors as $f): ?>
                        <li><?= e($f) ?></li>
                    <?php endforeach; ?>
                </ul>
            </div>
            <?php endif; ?>
        </div>

        <!-- 赛前分析备注 -->
        <?php if (!empty($preMatchNotes)): ?>
        <div class="evidence-card evidence-notes">
            <h3 class="evidence-title">📝 赛前分析</h3>
            <ul class="evidence-list">
                <?php foreach ($preMatchNotes as $n): ?>
                    <li><?= e($n) ?></li>
                <?php endforeach; ?>
            </ul>
        </div>
        <?php endif; ?>

        <!-- 模式匹配证据 -->
        <?php if (!empty($patternMatches)): ?>
        <div class="evidence-card evidence-patterns">
            <h3 class="evidence-title">🔍 模式匹配分析</h3>
            <?php foreach ($patternMatches as $pm): ?>
                <div class="pattern-match-item">
                    <div class="pm-header">
                        <span class="pm-type"><?= pattern_type_label($pm['pattern_type'] ?? '') ?></span>
                        <?php if (isset($pm['similarity'])): ?>
                            <span class="pm-similarity">相似度 <?= round($pm['similarity'], 1) ?>%</span>
                        <?php endif; ?>
                    </div>
                    <?php if (!empty($pm['description'])): ?>
                        <p class="pm-description"><?= e($pm['description']) ?></p>
                    <?php endif; ?>
                    <?php if (!empty($pm['evidence'])): ?>
                        <ul class="evidence-list pm-evidence">
                            <?php foreach ($pm['evidence'] as $ev): ?>
                                <li><?= e($ev) ?></li>
                            <?php endforeach; ?>
                        </ul>
                    <?php endif; ?>
                    <?php if (!empty($pm['conclusion'])): ?>
                        <div class="pm-conclusion">💡 <?= e($pm['conclusion']) ?></div>
                    <?php endif; ?>
                    <?php if (isset($pm['derived_line']) || isset($pm['market_line'])): ?>
                        <div class="pm-lines">
                            <?php if (isset($pm['derived_line'])): ?>
                                <span>推导盘口：<strong><?= e((string)$pm['derived_line']) ?></strong></span>
                            <?php endif; ?>
                            <?php if (isset($pm['market_line'])): ?>
                                <span>市场盘口：<strong><?= e((string)$pm['market_line']) ?></strong></span>
                            <?php endif; ?>
                        </div>
                    <?php endif; ?>
                </div>
            <?php endforeach; ?>
        </div>
        <?php endif; ?>

        <!-- 数据来源证据 -->
        <?php if (!empty($providerEvidence)): ?>
        <div class="evidence-card evidence-providers">
            <h3 class="evidence-title">📡 数据来源</h3>
            <ul class="evidence-list">
                <?php foreach ($providerEvidence as $pe): ?>
                    <li><?= e($pe) ?></li>
                <?php endforeach; ?>
            </ul>
        </div>
        <?php endif; ?>
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- 📊 详细数据 (折叠面板)                                                -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="section">
        <h2 class="section-title">📊 详细数据</h2>

        <div class="accordion">

            <!-- 凯利指数 -->
            <div class="accordion-item">
                <button class="accordion-trigger" onclick="this.parentElement.classList.toggle('open')">
                    <span>📈 凯利指数</span>
                    <span class="accordion-arrow">▾</span>
                </button>
                <div class="accordion-panel">
                    <?php if ($kellyCount > 0): ?>
                        <div class="kelly-grid">
                            <div class="kelly-card <?= kelly_color($kellyHome) ?>">
                                <div class="kelly-label">主胜</div>
                                <div class="kelly-value"><?= $kellyHome !== null ? number_format($kellyHome, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyHome) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyDraw) ?>">
                                <div class="kelly-label">平局</div>
                                <div class="kelly-value"><?= $kellyDraw !== null ? number_format($kellyDraw, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyDraw) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyAway) ?>">
                                <div class="kelly-label">客胜</div>
                                <div class="kelly-value"><?= $kellyAway !== null ? number_format($kellyAway, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyAway) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyOver) ?>">
                                <div class="kelly-label">大球</div>
                                <div class="kelly-value"><?= $kellyOver !== null ? number_format($kellyOver, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyOver) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyUnder) ?>">
                                <div class="kelly-label">小球</div>
                                <div class="kelly-value"><?= $kellyUnder !== null ? number_format($kellyUnder, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyUnder) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyAhH) ?>">
                                <div class="kelly-label">亚盘主</div>
                                <div class="kelly-value"><?= $kellyAhH !== null ? number_format($kellyAhH, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyAhH) ?></div>
                            </div>
                            <div class="kelly-card <?= kelly_color($kellyAhA) ?>">
                                <div class="kelly-label">亚盘客</div>
                                <div class="kelly-value"><?= $kellyAhA !== null ? number_format($kellyAhA, 4) : '--' ?></div>
                                <div class="kelly-status"><?= kelly_label($kellyAhA) ?></div>
                            </div>
                        </div>
                        <div class="kelly-source">基于 <?= $kellyCount ?> 家博彩公司数据计算</div>

                        <?php if (!empty($kellyDetails)): ?>
                        <div class="kelly-details-table-wrap">
                            <table class="kelly-details-table">
                                <thead>
                                    <tr>
                                        <th>博彩公司</th>
                                        <th>主胜</th>
                                        <th>平局</th>
                                        <th>客胜</th>
                                        <th>大球</th>
                                        <th>小球</th>
                                        <th>亚盘主</th>
                                        <th>亚盘客</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <?php foreach ($kellyDetails as $kd): ?>
                                    <tr>
                                        <td><?= e($kd['bookmaker'] ?? '--') ?></td>
                                        <td class="<?= kelly_color($kd['home_kelly'] ?? null) ?>"><?= isset($kd['home_kelly']) ? number_format($kd['home_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['draw_kelly'] ?? null) ?>"><?= isset($kd['draw_kelly']) ? number_format($kd['draw_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['away_kelly'] ?? null) ?>"><?= isset($kd['away_kelly']) ? number_format($kd['away_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['over_kelly'] ?? null) ?>"><?= isset($kd['over_kelly']) ? number_format($kd['over_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['under_kelly'] ?? null) ?>"><?= isset($kd['under_kelly']) ? number_format($kd['under_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['asian_home_kelly'] ?? null) ?>"><?= isset($kd['asian_home_kelly']) ? number_format($kd['asian_home_kelly'], 4) : '--' ?></td>
                                        <td class="<?= kelly_color($kd['asian_away_kelly'] ?? null) ?>"><?= isset($kd['asian_away_kelly']) ? number_format($kd['asian_away_kelly'], 4) : '--' ?></td>
                                    </tr>
                                    <?php endforeach; ?>
                                </tbody>
                            </table>
                        </div>
                        <?php endif; ?>
                    <?php else: ?>
                        <div class="text-muted">暂无凯利指数数据</div>
                    <?php endif; ?>
                </div>
            </div>

            <!-- 近期战绩 -->
            <?php
            $homeForm = $match['home_recent_form'] ?? null;
            $awayForm = $match['away_recent_form'] ?? null;
            ?>
            <?php if ($homeForm || $awayForm): ?>
            <div class="accordion-item">
                <button class="accordion-trigger" onclick="this.parentElement.classList.toggle('open')">
                    <span>📋 近期战绩</span>
                    <span class="accordion-arrow">▾</span>
                </button>
                <div class="accordion-panel">
                    <div class="form-grid">
                        <?php if ($homeForm): ?>
                        <div class="form-card">
                            <h4><?= e($match['home_team']['name'] ?? $match['home_team'] ?? '主队') ?> (主)</h4>
                            <?php if (!empty($homeForm['results'])): ?>
                                <div class="form-results">
                                    <?php foreach ($homeForm['results'] as $r): ?>
                                        <span class="form-badge form-<?= strtolower($r) ?>"><?= e($r) ?></span>
                                    <?php endforeach; ?>
                                </div>
                            <?php endif; ?>
                            <?php if (!empty($homeForm['summary'])): ?>
                                <p class="form-summary"><?= e($homeForm['summary']) ?></p>
                            <?php endif; ?>
                        </div>
                        <?php endif; ?>
                        <?php if ($awayForm): ?>
                        <div class="form-card">
                            <h4><?= e($match['away_team']['name'] ?? $match['away_team'] ?? '客队') ?> (客)</h4>
                            <?php if (!empty($awayForm['results'])): ?>
                                <div class="form-results">
                                    <?php foreach ($awayForm['results'] as $r): ?>
                                        <span class="form-badge form-<?= strtolower($r) ?>"><?= e($r) ?></span>
                                    <?php endforeach; ?>
                                </div>
                            <?php endif; ?>
                            <?php if (!empty($awayForm['summary'])): ?>
                                <p class="form-summary"><?= e($awayForm['summary']) ?></p>
                            <?php endif; ?>
                        </div>
                        <?php endif; ?>
                    </div>
                </div>
            </div>
            <?php endif; ?>

            <!-- 交锋历史 -->
            <?php $h2h = $match['h2h_summary'] ?? []; ?>
            <?php if (!empty($h2h)): ?>
            <div class="accordion-item">
                <button class="accordion-trigger" onclick="this.parentElement.classList.toggle('open')">
                    <span>🤝 交锋历史</span>
                    <span class="accordion-arrow">▾</span>
                </button>
                <div class="accordion-panel">
                    <ul class="h2h-list">
                        <?php foreach ($h2h as $h): ?>
                            <li class="h2h-item"><?= e($h) ?></li>
                        <?php endforeach; ?>
                    </ul>
                </div>
            </div>
            <?php endif; ?>

            <!-- 赔率对比 -->
            <?php
            $oddsData = $match['odds'] ?? null;
            $homeOdds = $oddsData['home'] ?? [];
            $drawOdds = $oddsData['draw'] ?? [];
            $awayOdds = $oddsData['away'] ?? [];
            $hasOdds = !empty($homeOdds) || !empty($drawOdds) || !empty($awayOdds);
            ?>
            <?php if ($hasOdds): ?>
            <div class="accordion-item">
                <button class="accordion-trigger" onclick="this.parentElement.classList.toggle('open')">
                    <span>💰 赔率对比</span>
                    <span class="accordion-arrow">▾</span>
                </button>
                <div class="accordion-panel">
                    <div class="odds-table-wrap">
                        <table class="odds-table">
                            <thead>
                                <tr>
                                    <th>博彩公司</th>
                                    <th>主胜</th>
                                    <th>平局</th>
                                    <th>客胜</th>
                                </tr>
                            </thead>
                            <tbody>
                                <?php
                                $maxRows = max(count($homeOdds), count($drawOdds), count($awayOdds));
                                for ($i = 0; $i < $maxRows; $i++):
                                    $ho = $homeOdds[$i] ?? null;
                                    $do = $drawOdds[$i] ?? null;
                                    $ao = $awayOdds[$i] ?? null;
                                    $bookmaker = $ho['bookmaker'] ?? $do['bookmaker'] ?? $ao['bookmaker'] ?? '--';
                                ?>
                                <tr>
                                    <td><?= e($bookmaker) ?></td>
                                    <td><?= isset($ho['price']) ? number_format($ho['price'], 2) : '--' ?></td>
                                    <td><?= isset($do['price']) ? number_format($do['price'], 2) : '--' ?></td>
                                    <td><?= isset($ao['price']) ? number_format($ao['price'], 2) : '--' ?></td>
                                </tr>
                                <?php endfor; ?>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            <?php endif; ?>
        </div>
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- 🔄 滚球预测 (仅进行中比赛)                                            -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <?php if ($live): ?>
    <section class="section" id="rolling-section">
        <h2 class="section-title">
            🔄 滚球预测
            <span class="live-dot"></span>
            <span class="refresh-indicator" id="rolling-refresh-indicator"></span>
        </h2>
        <div id="rolling-container">
            <?php if (!empty($rollingPreds)): ?>
                <div class="rolling-grid">
                    <?php foreach ($rollingPreds as $rp): ?>
                        <div class="rolling-card <?= confidence_class($rp['confidence_level'] ?? 'C') ?>">
                            <div class="rolling-card-header">
                                <span class="rolling-type-icon"><?= rolling_type_icon($rp['prediction_type'] ?? '') ?></span>
                                <span class="rolling-type"><?= rolling_type_label($rp['prediction_type'] ?? '') ?></span>
                                <span class="rolling-confidence <?= confidence_class($rp['confidence_level'] ?? 'C') ?>">
                                    <?= e(strtoupper($rp['confidence_level'] ?? 'C')) ?>
                                </span>
                            </div>
                            <div class="rolling-card-body">
                                <div class="rolling-label"><?= e(direction_badge($rp['prediction_type'] ?? '', $rp['label'] ?? '')) ?></div>
                                <?php if (isset($rp['probability'])): ?>
                                    <div class="rolling-prob"><?= round($rp['probability'] * 100) ?>%</div>
                                <?php endif; ?>
                            </div>
                            <?php if (!empty($rp['rationale'])): ?>
                                <div class="rolling-rationale"><?= e($rp['rationale']) ?></div>
                            <?php endif; ?>
                            <div class="rolling-card-footer">
                                <span><?= (int)($rp['minute'] ?? 0) ?>′</span>
                                <span><?= e($rp['current_score'] ?? '') ?></span>
                            </div>
                        </div>
                    <?php endforeach; ?>
                </div>
            <?php else: ?>
                <div class="text-muted">暂无滚球预测数据</div>
            <?php endif; ?>
        </div>
    </section>
    <?php endif; ?>

<?php endif; ?>

</main>

<footer class="footer">
    <div class="container">
        <p>足球智能预测系统 · 数据仅供参考</p>
    </div>
</footer>

<?php if ($live): ?>
<script>
(function() {
    var matchId = <?= json_encode($matchId) ?>;
    var container = document.getElementById('rolling-container');
    var indicator = document.getElementById('rolling-refresh-indicator');
    var polling = true;
    var timer = null;

    function fetchRolling() {
        if (!polling) return;
        indicator.textContent = '刷新中...';
        fetch('/api-proxy.php?endpoint=match/' + matchId + '/rolling')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                indicator.textContent = new Date().toLocaleTimeString();
                if (!Array.isArray(data) || data.length === 0) {
                    container.innerHTML = '<div class="text-muted">暂无滚球预测数据</div>';
                    return;
                }
                var typeLabels = {
                    'win_draw_lose': '胜平负',
                    'over_under': '大小球',
                    'asian_handicap': '亚盘',
                    'next_goal': '下一进球'
                };
                var typeIcons = {
                    'win_draw_lose': '⚽',
                    'over_under': '📊',
                    'asian_handicap': '🏆',
                    'next_goal': '🎯'
                };
                var html = '<div class="rolling-grid">';
                data.forEach(function(rp) {
                    var cls = 'confidence-' + (rp.confidence_level || 'C').toLowerCase();
                    var label = rp.label || '';
                    var prob = rp.probability ? Math.round(rp.probability * 100) + '%' : '';
                    html += '<div class="rolling-card ' + cls + '">';
                    html += '<div class="rolling-card-header">';
                    html += '<span class="rolling-type-icon">' + (typeIcons[rp.prediction_type] || '📌') + '</span>';
                    html += '<span class="rolling-type">' + (typeLabels[rp.prediction_type] || rp.prediction_type) + '</span>';
                    html += '<span class="rolling-confidence ' + cls + '">' + (rp.confidence_level || 'C').toUpperCase() + '</span>';
                    html += '</div>';
                    html += '<div class="rolling-card-body">';
                    html += '<div class="rolling-label">' + label + '</div>';
                    if (prob) html += '<div class="rolling-prob">' + prob + '</div>';
                    html += '</div>';
                    if (rp.rationale) html += '<div class="rolling-rationale">' + rp.rationale + '</div>';
                    html += '<div class="rolling-card-footer">';
                    html += '<span>' + (rp.minute || 0) + '\u2032</span>';
                    html += '<span>' + (rp.current_score || '') + '</span>';
                    html += '</div></div>';
                });
                html += '</div>';
                container.innerHTML = html;
            })
            .catch(function() {
                indicator.textContent = '刷新失败';
            });
    }

    timer = setInterval(fetchRolling, 30000);

    document.addEventListener('visibilitychange', function() {
        polling = !document.hidden;
        if (polling) fetchRolling();
    });
})();
</script>
<?php endif; ?>

</body>
</html>