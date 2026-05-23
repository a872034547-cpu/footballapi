<?php
/**
 * detail.php — 比赛详情页
 *
 * 功能：
 *   - URL 参数 ?match_id=xxx 指定比赛
 *   - 展示比赛基本信息、三大市场预测、模式匹配证据、滚球预测
 *   - 进行中比赛 AJAX 轮询实时比分
 */

declare(strict_types=1);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
init_db();
$db = db();

// ---------------------------------------------------------------------------
// 获取 match_id
// ---------------------------------------------------------------------------
$matchId = trim($_GET['match_id'] ?? '');
if ($matchId === '') {
    http_response_code(400);
    echo '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>缺少参数</title></head><body style="background:#030c18;color:#edf7ff;font-family:sans-serif;padding:40px;text-align:center;"><h1>⚠️ 缺少 match_id 参数</h1><p><a href="index.php" style="color:#7df48b;">← 返回首页</a></p></body></html>';
    exit;
}

// ---------------------------------------------------------------------------
// 查询比赛信息
// ---------------------------------------------------------------------------
$stmt = $db->prepare('SELECT * FROM matches WHERE match_id = :match_id');
$stmt->bindValue(':match_id', $matchId, SQLITE3_TEXT);
$result = $stmt->execute();
$match = $result->fetchArray(SQLITE3_ASSOC);

if (!$match) {
    http_response_code(404);
    echo '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>比赛不存在</title></head><body style="background:#030c18;color:#edf7ff;font-family:sans-serif;padding:40px;text-align:center;"><h1>🔍 比赛不存在</h1><p>match_id: ' . e($matchId) . '</p><p><a href="index.php" style="color:#7df48b;">← 返回首页</a></p></body></html>';
    exit;
}

// ---------------------------------------------------------------------------
// 查询该比赛的所有预测
// ---------------------------------------------------------------------------
$stmt = $db->prepare('
    SELECT p.*, pr.hit_status AS result_status, pr.profit, pr.roi, pr.result_text, pr.settled_at
    FROM predictions p
    LEFT JOIN prediction_results pr ON p.id = pr.prediction_id
    WHERE p.match_id = :match_id
    ORDER BY p.market, p.confidence DESC
');
$stmt->bindValue(':match_id', $matchId, SQLITE3_TEXT);
$predResult = $stmt->execute();

$predictions = [];
while ($row = $predResult->fetchArray(SQLITE3_ASSOC)) {
    $predictions[] = $row;
}

// ---------------------------------------------------------------------------
// 判断比赛状态
// ---------------------------------------------------------------------------
$matchStatus = strtoupper($match['status'] ?? 'SCHEDULED');
$isLive      = in_array($matchStatus, ['LIVE', 'HT'], true);
$isFinished  = in_array($matchStatus, ['FT', 'FINISHED'], true);
$isScheduled = in_array($matchStatus, ['SCHEDULED', 'NS'], true);

$homeScore = $match['home_score'] ?? null;
$awayScore = $match['away_score'] ?? null;
$minute    = 0; // 数据库中暂无 minute 字段，后续可从 API 获取

// ---------------------------------------------------------------------------
// 从 API 获取增强数据
// ---------------------------------------------------------------------------
$apiAnalysis   = null;
$rollingData   = null;
$apiError      = null;

try {
    if ($isLive) {
        // 进行中：调用滚球预测 API
        $hScore = (int)($homeScore ?? 0);
        $aScore = (int)($awayScore ?? 0);
        $rollingData = api_get("/match/{$matchId}/rolling?minute={$minute}&home_score={$hScore}&away_score={$aScore}");
    } elseif ($isScheduled) {
        // 未开赛：调用完整分析 API（含 pattern_matches）
        $apiAnalysis = api_get("/match/{$matchId}/analysis");
    }
    // 已结束比赛：仅展示本地数据，不调 API
} catch (RuntimeException $e) {
    $apiError = $e->getMessage();
}

// ---------------------------------------------------------------------------
// 辅助函数（复用 index.php 中的逻辑）
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

function format_kickoff(?string $kickoff): string
{
    if (empty($kickoff)) return '-';
    $ts = strtotime($kickoff);
    if ($ts === false) return e($kickoff);
    return date('Y-m-d H:i', $ts);
}

function display_score(?int $home, ?int $away): string
{
    if ($home === null || $away === null) return 'vs';
    return "{$home} : {$away}";
}

function match_status_text(string $status): string
{
    return match (strtoupper($status)) {
        'SCHEDULED', 'NS' => '未开赛',
        'LIVE'            => '● 进行中',
        'HT'              => '⏸ 中场休息',
        'FT', 'FINISHED'  => '已结束',
        default           => $status,
    };
}

function confidence_level_class(string $level): string
{
    return match (strtoupper($level)) {
        'A' => 'badge-hit',
        'B' => 'badge-hit',
        'C' => 'badge-pending',
        'D' => 'badge-miss',
        default => '',
    };
}

function confidence_level_text(string $level): string
{
    return match (strtoupper($level)) {
        'A' => '高信心',
        'B' => '较高信心',
        'C' => '中等信心',
        'D' => '低信心',
        default => $level,
    };
}

// ---------------------------------------------------------------------------
// 按市场分组本地预测
// ---------------------------------------------------------------------------
$localByMarket = [];
foreach ($predictions as $p) {
    $mkt = $p['market'];
    if (!isset($localByMarket[$mkt])) {
        $localByMarket[$mkt] = [];
    }
    $localByMarket[$mkt][] = $p;
}

// ---------------------------------------------------------------------------
// 从 API 分析结果中提取各市场预测
// ---------------------------------------------------------------------------
$apiWinDrawLose  = $apiAnalysis['win_draw_lose']  ?? [];
$apiOverUnder    = $apiAnalysis['over_under']      ?? [];
$apiAsianHcp     = $apiAnalysis['asian_handicap']  ?? [];
$apiPatterns     = $apiAnalysis['pattern_matches'] ?? [];
$apiConfidence   = $apiAnalysis['overall_confidence_level'] ?? '';
$apiBenefit      = $apiAnalysis['benefit_factors'] ?? [];
$apiRisk         = $apiAnalysis['risk_factors']    ?? [];
$apiPredScores   = $apiAnalysis['predicted_scores'] ?? [];

?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= e($match['home_team']) ?> vs <?= e($match['away_team']) ?> — 比赛详情</title>
    <link rel="stylesheet" href="style.css">
    <style>
        /* ── 详情页补充样式 ── */
        .detail-nav {
            padding: 16px 0;
            border-bottom: 1px solid rgba(255,255,255,0.06);
            margin-bottom: 24px;
        }
        .detail-nav a {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-weight: 700;
            font-size: 14px;
            color: #8ea3bd;
            transition: color 0.2s;
        }
        .detail-nav a:hover {
            color: #7df48b;
            text-decoration: none;
        }

        /* 比赛信息大卡片 */
        .match-hero-card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 20px;
            padding: 32px 24px;
            text-align: center;
            margin-bottom: 28px;
            backdrop-filter: blur(10px);
        }
        .match-hero-card .league-tag {
            display: inline-block;
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #8ea3bd;
            background: rgba(255,255,255,0.06);
            padding: 4px 14px;
            border-radius: 999px;
            margin-bottom: 16px;
        }
        .match-hero-card .teams-row {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: clamp(16px, 4vw, 40px);
            flex-wrap: wrap;
        }
        .match-hero-card .team-name {
            font-size: clamp(20px, 4vw, 32px);
            font-weight: 900;
            color: #fff;
            min-width: 120px;
        }
        .match-hero-card .score-display {
            font-size: clamp(36px, 7vw, 56px);
            font-weight: 950;
            color: #7df48b;
            line-height: 1;
            letter-spacing: 0.02em;
            min-width: 100px;
        }
        .match-hero-card .score-display.live-score {
            animation: scorePulse 2s ease-in-out infinite;
        }
        @keyframes scorePulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .match-hero-card .status-tag {
            display: inline-block;
            margin-top: 16px;
            font-size: 13px;
            font-weight: 800;
            padding: 6px 18px;
            border-radius: 999px;
        }
        .match-hero-card .status-tag.live {
            background: rgba(125,244,139,0.16);
            color: #7df48b;
            border: 1px solid rgba(125,244,139,0.30);
        }
        .match-hero-card .status-tag.scheduled {
            background: rgba(255,193,7,0.12);
            color: #ffc107;
            border: 1px solid rgba(255,193,7,0.25);
        }
        .match-hero-card .status-tag.finished {
            background: rgba(142,163,189,0.12);
            color: #8ea3bd;
            border: 1px solid rgba(142,163,189,0.25);
        }
        .match-hero-card .kickoff-info {
            margin-top: 12px;
            font-size: 13px;
            color: #8ea3bd;
        }

        /* 预测详情卡片 */
        .prediction-detail-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            padding: 24px;
            margin-bottom: 20px;
            backdrop-filter: blur(10px);
        }
        .prediction-detail-card .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 10px;
        }
        .prediction-detail-card .card-header h3 {
            margin: 0;
            font-size: 18px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .prediction-detail-card .prediction-list {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }
        .prediction-detail-card .pred-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 14px 16px;
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.05);
            flex-wrap: wrap;
            gap: 10px;
        }
        .prediction-detail-card .pred-item.primary {
            border-color: rgba(125,244,139,0.25);
            background: rgba(125,244,139,0.06);
        }
        .prediction-detail-card .pred-label {
            font-weight: 800;
            color: #fff;
            font-size: 15px;
        }
        .prediction-detail-card .pred-probability {
            font-weight: 900;
            font-size: 20px;
            color: #7df48b;
            min-width: 60px;
            text-align: right;
        }
        .prediction-detail-card .pred-rationale {
            width: 100%;
            font-size: 13px;
            color: #8ea3bd;
            line-height: 1.6;
            padding-top: 8px;
            border-top: 1px solid rgba(255,255,255,0.04);
        }

        /* 概率条 */
        .prob-bar-wrap {
            flex: 1;
            min-width: 120px;
            height: 8px;
            background: rgba(255,255,255,0.06);
            border-radius: 999px;
            overflow: hidden;
            margin: 0 12px;
        }
        .prob-bar-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, #7df48b, #33b8ff);
            transition: width 0.6s ease;
        }

        /* 折叠面板 */
        .accordion {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            margin-bottom: 20px;
            overflow: hidden;
        }
        .accordion-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 18px 24px;
            cursor: pointer;
            user-select: none;
            transition: background 0.2s;
        }
        .accordion-header:hover {
            background: rgba(255,255,255,0.03);
        }
        .accordion-header h3 {
            margin: 0;
            font-size: 16px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .accordion-header .arrow {
            font-size: 12px;
            color: #8ea3bd;
            transition: transform 0.3s;
        }
        .accordion.open .accordion-header .arrow {
            transform: rotate(180deg);
        }
        .accordion-body {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.35s ease;
        }
        .accordion.open .accordion-body {
            max-height: 2000px;
        }
        .accordion-body-inner {
            padding: 0 24px 20px;
        }

        /* 模式匹配证据项 */
        .pattern-item {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .pattern-item .pattern-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 10px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .pattern-item .pattern-type {
            font-weight: 800;
            font-size: 13px;
            color: #7df48b;
            text-transform: uppercase;
        }
        .pattern-item .pattern-similarity {
            font-weight: 800;
            font-size: 13px;
            color: #8ea3bd;
        }
        .pattern-item .pattern-desc {
            font-size: 13px;
            color: #edf7ff;
            margin-bottom: 8px;
        }
        .pattern-item .pattern-evidence {
            list-style: none;
            padding: 0;
            margin: 0;
        }
        .pattern-item .pattern-evidence li {
            font-size: 12px;
            color: #8ea3bd;
            padding: 3px 0;
            padding-left: 16px;
            position: relative;
        }
        .pattern-item .pattern-evidence li::before {
            content: "•";
            position: absolute;
            left: 0;
            color: #7df48b;
        }
        .pattern-item .pattern-conclusion {
            margin-top: 10px;
            padding: 10px 14px;
            background: rgba(125,244,139,0.06);
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            color: #7df48b;
        }

        /* 滚球预测卡片 */
        .rolling-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(125,244,139,0.20);
            border-radius: 18px;
            padding: 20px;
            margin-bottom: 16px;
            backdrop-filter: blur(10px);
        }
        .rolling-card .rolling-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .rolling-card .rolling-type {
            font-weight: 800;
            font-size: 13px;
            color: #7df48b;
            text-transform: uppercase;
        }
        .rolling-card .rolling-label {
            font-weight: 900;
            font-size: 16px;
            color: #fff;
        }
        .rolling-card .rolling-prob {
            font-weight: 900;
            font-size: 22px;
            color: #7df48b;
        }
        .rolling-card .rolling-rationale {
            font-size: 13px;
            color: #8ea3bd;
            line-height: 1.6;
            margin-top: 8px;
            padding-top: 10px;
            border-top: 1px solid rgba(255,255,255,0.05);
        }

        /* 利好/风险因子 */
        .factor-list {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 12px;
        }
        .factor-tag {
            font-size: 12px;
            font-weight: 700;
            padding: 4px 12px;
            border-radius: 999px;
        }
        .factor-tag.benefit {
            background: rgba(125,244,139,0.12);
            color: #7df48b;
            border: 1px solid rgba(125,244,139,0.25);
        }
        .factor-tag.risk {
            background: rgba(255,90,90,0.12);
            color: #ff6b6b;
            border: 1px solid rgba(255,90,90,0.25);
        }

        /* 比分预测 */
        .score-pred-list {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 12px;
        }
        .score-pred-item {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            padding: 8px 16px;
            text-align: center;
            font-weight: 800;
            font-size: 14px;
        }
        .score-pred-item .sp-score {
            color: #fff;
        }
        .score-pred-item .sp-prob {
            color: #7df48b;
            font-size: 12px;
        }

        /* 无数据 */
        .no-data {
            text-align: center;
            padding: 40px 20px;
            color: #8ea3bd;
            font-size: 14px;
        }

        /* API 错误提示 */
        .api-error {
            background: rgba(255,90,90,0.08);
            border: 1px solid rgba(255,90,90,0.20);
            border-radius: 12px;
            padding: 12px 16px;
            margin-bottom: 20px;
            font-size: 13px;
            color: #ff6b6b;
        }

        /* 响应式 */
        @media (max-width: 600px) {
            .match-hero-card .teams-row {
                flex-direction: column;
                gap: 12px;
            }
            .prediction-detail-card .pred-item {
                flex-direction: column;
                align-items: flex-start;
            }
            .prob-bar-wrap {
                width: 100%;
                margin: 4px 0;
            }
        }
    </style>
</head>
<body>

<!-- ═══════════════════════════════════════════ -->
<!--  顶部导航                                    -->
<!-- ═══════════════════════════════════════════ -->
<div class="container">
    <nav class="detail-nav">
        <a href="index.php">← 返回首页</a>
    </nav>
</div>

<!-- ═══════════════════════════════════════════ -->
<!--  比赛信息大卡片                              -->
<!-- ═══════════════════════════════════════════ -->
<section class="container">
    <div class="match-hero-card" id="matchHero">
        <div class="league-tag"><?= e($match['league']) ?></div>
        <div class="teams-row">
            <span class="team-name"><?= e($match['home_team']) ?></span>
            <span class="score-display <?= $isLive ? 'live-score' : '' ?>" id="liveScore">
                <?= display_score(
                    $homeScore !== null ? (int)$homeScore : null,
                    $awayScore !== null ? (int)$awayScore : null
                ) ?>
            </span>
            <span class="team-name"><?= e($match['away_team']) ?></span>
        </div>
        <div>
            <span class="status-tag <?= $isLive ? 'live' : ($isFinished ? 'finished' : 'scheduled') ?>">
                <?= match_status_text($matchStatus) ?>
            </span>
        </div>
        <div class="kickoff-info">
            📅 开赛时间：<?= format_kickoff($match['kickoff']) ?>
        </div>
    </div>
</section>

<!-- ═══════════════════════════════════════════ -->
<!--  API 错误提示                               -->
<!-- ═══════════════════════════════════════════ -->
<?php if ($apiError !== null): ?>
<section class="container">
    <div class="api-error">
        ⚠️ API 数据获取失败：<?= e($apiError) ?>（以下展示本地缓存数据）
    </div>
</section>
<?php endif; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  综合信心 & 因子                            -->
<!-- ═══════════════════════════════════════════ -->
<?php if ($apiConfidence !== '' || !empty($apiBenefit) || !empty($apiRisk)): ?>
<section class="container">
    <div class="prediction-detail-card">
        <div class="card-header">
            <h3>📊 综合分析</h3>
            <?php if ($apiConfidence !== ''): ?>
            <span class="<?= confidence_level_class($apiConfidence) ?>">
                <?= confidence_level_text($apiConfidence) ?> (<?= e($apiConfidence) ?>)
            </span>
            <?php endif; ?>
        </div>
        <?php if (!empty($apiBenefit)): ?>
        <div style="margin-bottom: 8px;">
            <strong style="font-size:13px;color:#7df48b;">✅ 利好因子</strong>
            <div class="factor-list">
                <?php foreach ($apiBenefit as $f): ?>
                <span class="factor-tag benefit"><?= e($f) ?></span>
                <?php endforeach; ?>
            </div>
        </div>
        <?php endif; ?>
        <?php if (!empty($apiRisk)): ?>
        <div>
            <strong style="font-size:13px;color:#ff6b6b;">⚠️ 风险因子</strong>
            <div class="factor-list">
                <?php foreach ($apiRisk as $f): ?>
                <span class="factor-tag risk"><?= e($f) ?></span>
                <?php endforeach; ?>
            </div>
        </div>
        <?php endif; ?>
        <?php if (!empty($apiPredScores)): ?>
        <div style="margin-top: 14px;">
            <strong style="font-size:13px;color:#8ea3bd;">🎯 比分预测</strong>
            <div class="score-pred-list">
                <?php foreach ($apiPredScores as $sp): ?>
                <div class="score-pred-item">
                    <span class="sp-score"><?= e($sp['score'] ?? '') ?></span>
                    <br>
                    <span class="sp-prob"><?= percent_fmt($sp['probability'] ?? 0) ?></span>
                </div>
                <?php endforeach; ?>
            </div>
        </div>
        <?php endif; ?>
    </div>
</section>
<?php endif; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  三大市场预测详情                            -->
<!-- ═══════════════════════════════════════════ -->

<?php
// 构建市场数据：优先使用 API 数据，回退到本地数据
$markets = [
    '1x2' => [
        'icon'   => '⚽',
        'title'  => '胜平负预测',
        'api'    => $apiWinDrawLose,
        'local'  => $localByMarket['1x2'] ?? [],
    ],
    'over_under' => [
        'icon'   => '📐',
        'title'  => '大小球预测',
        'api'    => $apiOverUnder,
        'local'  => $localByMarket['over_under'] ?? [],
    ],
    'asian_handicap' => [
        'icon'   => '📏',
        'title'  => '亚盘预测',
        'api'    => $apiAsianHcp,
        'local'  => $localByMarket['asian_handicap'] ?? [],
    ],
];

foreach ($markets as $mktKey => $mktData):
    $apiPreds  = $mktData['api'];
    $localPreds = $mktData['local'];

    // 找到最高概率项作为 primary
    $maxProb = 0.0;
    $primaryIdx = -1;
    foreach ($apiPreds as $i => $ap) {
        $prob = (float)($ap['probability'] ?? 0);
        if ($prob > $maxProb) {
            $maxProb = $prob;
            $primaryIdx = $i;
        }
    }
?>
<section class="container">
    <div class="prediction-detail-card">
        <div class="card-header">
            <h3><?= $mktData['icon'] ?> <?= $mktData['title'] ?></h3>
            <?php if (!empty($localPreds)): ?>
            <span style="font-size:12px;color:#8ea3bd;">
                <?= count($localPreds) ?> 条本地记录
            </span>
            <?php endif; ?>
        </div>

        <?php if (!empty($apiPreds)): ?>
        <!-- API 预测数据 -->
        <div class="prediction-list">
            <?php foreach ($apiPreds as $i => $ap):
                $prob  = (float)($ap['probability'] ?? 0);
                $label = $ap['label'] ?? '';
                $rationale = $ap['rationale'] ?? '';
                $isPrimary = ($i === $primaryIdx && $maxProb > 0);
            ?>
            <div class="pred-item <?= $isPrimary ? 'primary' : '' ?>">
                <span class="pred-label">
                    <?= $isPrimary ? '⭐ ' : '' ?><?= e($label) ?>
                </span>
                <div class="prob-bar-wrap">
                    <div class="prob-bar-fill" style="width:<?= min($prob, 100) ?>%;"></div>
                </div>
                <span class="pred-probability"><?= percent_fmt($prob) ?></span>
                <?php if ($rationale !== ''): ?>
                <div class="pred-rationale"><?= e($rationale) ?></div>
                <?php endif; ?>
            </div>
            <?php endforeach; ?>
        </div>
        <?php elseif (!empty($localPreds)): ?>
        <!-- 回退：本地预测数据 -->
        <div class="prediction-list">
            <?php foreach ($localPreds as $lp):
                $conf = (float)($lp['confidence'] ?? 0);
                $dir  = $lp['direction'] ?? '';
                $odds = $lp['odds'] ?? null;
                $summary = $lp['free_summary'] ?? '';
            ?>
            <div class="pred-item">
                <span class="pred-label"><?= direction_badge($mktKey, $dir) ?></span>
                <div class="prob-bar-wrap">
                    <div class="prob-bar-fill" style="width:<?= min($conf, 100) ?>%;"></div>
                </div>
                <span class="pred-probability"><?= number_format($conf, 1) ?>%</span>
                <?php if ($odds): ?>
                <span style="font-size:13px;color:#8ea3bd;">赔率 <?= number_format((float)$odds, 2) ?></span>
                <?php endif; ?>
                <?php if ($summary !== '' && $summary !== null): ?>
                <div class="pred-rationale"><?= e($summary) ?></div>
                <?php endif; ?>
            </div>
            <?php endforeach; ?>
        </div>
        <?php else: ?>
        <div class="no-data">暂无该市场预测数据</div>
        <?php endif; ?>
    </div>
</section>
<?php endforeach; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  模式匹配证据（折叠面板）                     -->
<!-- ═══════════════════════════════════════════ -->
<?php if (!empty($apiPatterns)): ?>
<section class="container">
    <div class="accordion" id="patternAccordion">
        <div class="accordion-header" onclick="toggleAccordion('patternAccordion')">
            <h3>🔬 历史模式匹配证据 <span style="font-size:12px;color:#8ea3bd;font-weight:400;">(<?= count($apiPatterns) ?> 条)</span></h3>
            <span class="arrow">▼</span>
        </div>
        <div class="accordion-body">
            <div class="accordion-body-inner">
                <?php foreach ($apiPatterns as $pm):
                    $patternType = $pm['pattern_type'] ?? '';
                    $desc        = $pm['description'] ?? '';
                    $similarity  = (float)($pm['similarity'] ?? 0);
                    $evidence    = $pm['evidence'] ?? [];
                    $derivedLine = $pm['derived_line'] ?? null;
                    $marketLine  = $pm['market_line'] ?? null;
                    $conclusion  = $pm['conclusion'] ?? '';
                ?>
                <div class="pattern-item">
                    <div class="pattern-header">
                        <span class="pattern-type">
                            <?= match ($patternType) {
                                'handicap_derivation' => '📐 让球推导',
                                'odds_implied'        => '📊 欧赔隐含概率',
                                'asian_water'         => '💧 亚盘水位分析',
                                'totals'              => '⚽ 大小球预期',
                                default               => e($patternType),
                            } ?>
                        </span>
                        <span class="pattern-similarity">
                            相似度：<?= number_format($similarity, 1) ?>%
                        </span>
                    </div>
                    <?php if ($desc !== ''): ?>
                    <div class="pattern-desc"><?= e($desc) ?></div>
                    <?php endif; ?>
                    <?php if ($derivedLine !== null || $marketLine !== null): ?>
                    <div style="font-size:12px;color:#8ea3bd;margin-bottom:8px;">
                        <?php if ($derivedLine !== null): ?>
                        推导盘口：<strong style="color:#7df48b;"><?= e((string)$derivedLine) ?></strong>
                        <?php endif; ?>
                        <?php if ($marketLine !== null): ?>
                        &nbsp;|&nbsp; 市场盘口：<strong style="color:#edf7ff;"><?= e((string)$marketLine) ?></strong>
                        <?php endif; ?>
                    </div>
                    <?php endif; ?>
                    <?php if (!empty($evidence)): ?>
                    <ul class="pattern-evidence">
                        <?php foreach ($evidence as $ev): ?>
                        <li><?= e($ev) ?></li>
                        <?php endforeach; ?>
                    </ul>
                    <?php endif; ?>
                    <?php if ($conclusion !== ''): ?>
                    <div class="pattern-conclusion">💡 <?= e($conclusion) ?></div>
                    <?php endif; ?>
                </div>
                <?php endforeach; ?>
            </div>
        </div>
    </div>
</section>
<?php endif; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  滚球预测区（仅进行中比赛）                   -->
<!-- ═══════════════════════════════════════════ -->
<?php if ($isLive): ?>
<section class="container" id="rollingSection">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
        <h2 style="margin:0;">🔄 滚球实时预测</h2>
        <span style="font-size:12px;color:#8ea3bd;" id="rollingUpdateTime"></span>
    </div>

    <div id="rollingContainer">
        <?php if (!empty($rollingData)): ?>
            <?php foreach ($rollingData as $rp):
                $rpType    = $rp['prediction_type'] ?? '';
                $rpLabel   = $rp['label'] ?? '';
                $rpProb    = (float)($rp['probability'] ?? 0);
                $rpRationale = $rp['rationale'] ?? '';
                $rpConf    = $rp['confidence_level'] ?? 'C';
            ?>
            <div class="rolling-card">
                <div class="rolling-header">
                    <span class="rolling-type">
                        <?= match ($rpType) {
                            'win_draw_lose'   => '⚽ 胜平负',
                            'over_under'      => '📐 大小球',
                            'asian_handicap'  => '📏 亚盘',
                            'next_goal'       => '🎯 下一进球方',
                            default           => e($rpType),
                        } ?>
                    </span>
                    <span class="<?= confidence_level_class($rpConf) ?>">
                        <?= confidence_level_text($rpConf) ?>
                    </span>
                </div>
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
                    <span class="rolling-label"><?= e($rpLabel) ?></span>
                    <span class="rolling-prob"><?= percent_fmt($rpProb) ?></span>
                </div>
                <?php if ($rpRationale !== ''): ?>
                <div class="rolling-rationale"><?= e($rpRationale) ?></div>
                <?php endif; ?>
            </div>
            <?php endforeach; ?>
        <?php else: ?>
        <div class="no-data">滚球预测数据加载中...</div>
        <?php endif; ?>
    </div>
</section>
<?php endif; ?>

<!-- ═══════════════════════════════════════════ -->
<!--  页脚                                        -->
<!-- ═══════════════════════════════════════════ -->
<footer style="text-align:center;padding:40px 20px;color:#8ea3bd;font-size:12px;border-top:1px solid rgba(255,255,255,0.06);margin-top:40px;">
    <p>数据来源：football-data.org · The Odds API · Goalserve · 500.com · 足彩网 · njstats.cn</p>
    <p>预测仅供参考，不构成投注建议。</p>
</footer>

<!-- ═══════════════════════════════════════════ -->
<!--  JavaScript                                -->
<!-- ═══════════════════════════════════════════ -->
<script>
// ── 折叠面板切换 ──
function toggleAccordion(id) {
    var el = document.getElementById(id);
    if (el) {
        el.classList.toggle('open');
    }
}

<?php if ($isLive): ?>
// ── 实时比分 AJAX 轮询 ──
(function() {
    var matchId    = <?= json_encode($matchId, JSON_UNESCAPED_SLASHES) ?>;
    var pollTimer  = null;
    var POLL_INTERVAL = 30000; // 30 秒

    function updateLiveScore() {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '<?= e(config('api.base_url')) ?>/match/' + encodeURIComponent(matchId) + '/rolling?minute=0&home_score=0&away_score=0');
        xhr.timeout = 10000;
        xhr.onload = function() {
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    var data = JSON.parse(xhr.responseText);
                    // 更新滚球预测卡片
                    var container = document.getElementById('rollingContainer');
                    if (container && Array.isArray(data) && data.length > 0) {
                        var html = '';
                        data.forEach(function(rp) {
                            var typeMap = {
                                'win_draw_lose': '⚽ 胜平负',
                                'over_under': '📐 大小球',
                                'asian_handicap': '📏 亚盘',
                                'next_goal': '🎯 下一进球方'
                            };
                            var confClass = '';
                            var confText = '';
                            switch ((rp.confidence_level || 'C').toUpperCase()) {
                                case 'A': confClass = 'badge-hit'; confText = '高信心'; break;
                                case 'B': confClass = 'badge-hit'; confText = '较高信心'; break;
                                case 'C': confClass = 'badge-pending'; confText = '中等信心'; break;
                                case 'D': confClass = 'badge-miss'; confText = '低信心'; break;
                                default: confClass = ''; confText = rp.confidence_level || '';
                            }
                            var prob = parseFloat(rp.probability || 0);
                            var probStr = (prob > 0 && prob < 1 ? (prob * 100) : prob).toFixed(1) + '%';
                            html += '<div class="rolling-card">' +
                                '<div class="rolling-header">' +
                                    '<span class="rolling-type">' + (typeMap[rp.prediction_type] || rp.prediction_type) + '</span>' +
                                    '<span class="' + confClass + '">' + confText + '</span>' +
                                '</div>' +
                                '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">' +
                                    '<span class="rolling-label">' + escapeHtml(rp.label || '') + '</span>' +
                                    '<span class="rolling-prob">' + probStr + '</span>' +
                                '</div>' +
                                (rp.rationale ? '<div class="rolling-rationale">' + escapeHtml(rp.rationale) + '</div>' : '') +
                            '</div>';
                        });
                        container.innerHTML = html;
                    }

                    // 更新更新时间
                    var timeEl = document.getElementById('rollingUpdateTime');
                    if (timeEl) {
                        var now = new Date();
                        timeEl.textContent = '更新于 ' + now.toLocaleTimeString('zh-CN');
                    }
                } catch (e) {
                    console.error('滚球数据解析失败:', e);
                }
            }
        };
        xhr.onerror = function() {
            console.error('滚球数据请求失败');
        };
        xhr.send();
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // 首次加载后启动轮询
    updateLiveScore();
    pollTimer = setInterval(updateLiveScore, POLL_INTERVAL);

    // 页面隐藏时暂停轮询，可见时恢复
    document.addEventListener('visibilitychange', function() {
        if (document.hidden) {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        } else {
            updateLiveScore();
            if (!pollTimer) {
                pollTimer = setInterval(updateLiveScore, POLL_INTERVAL);
            }
        }
    });
})();
<?php endif; ?>
</script>

</body>
</html>