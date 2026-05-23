<?php
/**
 * settle.php — 预测结算脚本（CLI）
 *
 * 用法：
 *   php settle.php                # 结算所有 pending 预测
 *   php settle.php --dry-run      # 预览模式，不写入数据库
 *
 * 流程：
 *   1. 查询所有 status='pending' 的预测
 *   2. 逐条获取比赛快照，检查是否已完赛
 *   3. 根据比分结算：hit / miss / void
 *   4. 写入 prediction_results 表
 *   5. 更新 predictions.status = 'settled'
 *
 * 结算规则（参考 0521 PredictionService）：
 *   - 1x2: 比较实际赛果（主胜/平/客胜）与预测方向
 *   - over_under: total = home + away, 比较 total 与预测线
 *   - asian_handicap: 比较 (home + line) 与 away
 */

declare(strict_types=1);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 参数解析
// ---------------------------------------------------------------------------
$dryRun = in_array('--dry-run', $argv, true);

echo "══════════════════════════════════════════\n";
echo "  预测结算" . ($dryRun ? " [预览模式]" : "") . "\n";
echo "  API: " . config('api.base_url') . "\n";
echo "══════════════════════════════════════════\n\n";

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
init_db();
$db       = db();
$stakeAmt = (float) config('predict.stake_amount', 1000);

// ---------------------------------------------------------------------------
// 查询待结算预测
// ---------------------------------------------------------------------------
$pending = $db->query("
    SELECT p.*, m.home_team, m.away_team, m.status AS match_status,
           m.home_score, m.away_score, m.score
    FROM predictions p
    JOIN matches m ON p.match_id = m.match_id
    WHERE p.status = 'pending'
    ORDER BY p.created_at
");

$rows = [];
while ($row = $pending->fetchArray(SQLITE3_ASSOC)) {
    $rows[] = $row;
}

if (empty($rows)) {
    echo "  没有待结算的预测。\n";
    exit(0);
}

echo "  待结算预测: " . count($rows) . " 条\n\n";

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

/**
 * 判断比赛是否已完赛
 */
function is_match_finished(string $status): bool
{
    $finishedKeywords = ['完', 'FT', 'Finished', '终', '结束', '全场'];
    foreach ($finishedKeywords as $kw) {
        if (stripos($status, $kw) !== false) {
            return true;
        }
    }
    return false;
}

/**
 * 从比分字符串解析主客队进球
 */
function parse_score(?string $score): array
{
    if (empty($score)) return [null, null];
    // 支持 "2:1", "2-1", "2 - 1" 等格式
    if (preg_match('/(\d+)\s*[-:]\s*(\d+)/', $score, $m)) {
        return [(int) $m[1], (int) $m[2]];
    }
    return [null, null];
}

/**
 * 结算 1x2 预测
 */
function settle_1x2(array $pred, int $home, int $away): array
{
    $actual = ($home > $away) ? 'home' : (($home < $away) ? 'away' : 'draw');
    $dir    = strtolower(trim($pred['direction']));

    // 标准化方向
    $dirMap = [
        'home' => 'home', '主胜' => 'home', '1' => 'home', 'win' => 'home',
        'away' => 'away', '客胜' => 'away', '2' => 'away',
        'draw' => 'draw', '平'  => 'draw', 'x' => 'draw',
    ];

    $normalized = $dirMap[$dir] ?? $dir;

    if ($normalized === $actual) {
        return ['hit', "{$home}:{$away} → 预测{$pred['direction']} ✓"];
    }
    return ['miss', "{$home}:{$away} → 预测{$pred['direction']} ✗ (实际{$actual})"];
}

/**
 * 结算大小球预测
 */
function settle_over_under(array $pred, int $home, int $away): array
{
    $total = $home + $away;
    $dir   = strtolower(trim($pred['direction']));

    // 从 direction 中提取线（如 "over_2.5", "大2.5", "under_2.5", "小2.5"）
    $line = null;
    if (preg_match('/(\d+\.?\d*)/', $dir, $m)) {
        $line = (float) $m[1];
    }

    $isOver = (stripos($dir, 'over') !== false || strpos($dir, '大') !== false);

    if ($line === null) {
        return ['void', "{$home}:{$away} (总{$total}) → 无法解析盘口线"];
    }

    if ($total > $line) {
        $actualResult = 'over';
    } elseif ($total < $line) {
        $actualResult = 'under';
    } else {
        return ['void', "{$home}:{$away} (总{$total}) → 走水 (线{$line})"];
    }

    if (($isOver && $actualResult === 'over') || (!$isOver && $actualResult === 'under')) {
        return ['hit', "{$home}:{$away} (总{$total}) → 预测{$pred['direction']} ✓"];
    }
    return ['miss', "{$home}:{$away} (总{$total}) → 预测{$pred['direction']} ✗"];
}

/**
 * 结算亚盘预测
 */
function settle_asian_handicap(array $pred, int $home, int $away): array
{
    $dir  = strtolower(trim($pred['direction']));
    $line = null;

    // 从 direction 提取盘口线
    if (preg_match('/[-+]?\d+\.?\d*/', $dir, $m)) {
        $line = (float) $m[0];
    }

    $isAway = (stripos($dir, 'away') !== false || strpos($dir, '客') !== false);

    if ($line === null) {
        return ['void', "{$home}:{$away} → 无法解析盘口线"];
    }

    // scoreDiff = isAway ? (away - home - line) : (home + line - away)
    $scoreDiff = $isAway ? ($away - $home - $line) : ($home + $line - $away);

    if ($scoreDiff > 0) {
        return ['hit', "{$home}:{$away} (盘口{$line}) → 预测{$pred['direction']} ✓"];
    } elseif ($scoreDiff < 0) {
        return ['miss', "{$home}:{$away} (盘口{$line}) → 预测{$pred['direction']} ✗"];
    }
    return ['void', "{$home}:{$away} (盘口{$line}) → 走水"];
}

// ---------------------------------------------------------------------------
// 逐条结算
// ---------------------------------------------------------------------------
$stmtResult = $db->prepare("
    INSERT OR REPLACE INTO prediction_results (prediction_id, hit_status, stake, profit, roi, result_text, settled_at)
    VALUES (:pid, :status, :stake, :profit, :roi, :text, CURRENT_TIMESTAMP)
");

$stmtUpdatePred = $db->prepare("
    UPDATE predictions SET status = 'settled' WHERE id = :pid
");

$hit   = 0;
$miss  = 0;
$void  = 0;
$total = 0;

foreach ($rows as $row) {
    $predId     = $row['id'];
    $matchId    = $row['match_id'];
    $market     = $row['market'];
    $homeTeam   = $row['home_team'];
    $awayTeam   = $row['away_team'];
    $matchStatus = $row['match_status'] ?? '';

    // 先检查本地数据库中的比分
    $homeScore = $row['home_score'];
    $awayScore = $row['away_score'];
    $scoreStr  = $row['score'];

    // 如果本地没有比分，尝试从 API 获取
    if ($homeScore === null || $awayScore === null) {
        try {
            $snapshot = api_get("match/{$matchId}/snapshot");
            $homeScore = $snapshot['home_score'] ?? null;
            $awayScore = $snapshot['away_score'] ?? null;
            $scoreStr  = $snapshot['score']      ?? null;
            $matchStatus = $snapshot['status']   ?? $matchStatus;

            // 更新本地比分
            if ($homeScore !== null && $awayScore !== null) {
                $db->exec("UPDATE matches SET home_score = " . (int)$homeScore .
                          ", away_score = " . (int)$awayScore .
                          ", score = " . ($scoreStr ? "'" . $db->escapeString($scoreStr) . "'" : "NULL") .
                          ", status = '" . $db->escapeString($matchStatus) . "'" .
                          ", updated_at = CURRENT_TIMESTAMP WHERE match_id = '" . $db->escapeString($matchId) . "'");
            }
        } catch (RuntimeException $e) {
            echo "  ⚠ 获取快照失败 [{$matchId}]: {$e->getMessage()}\n";
            continue;
        }
    }

    // 检查是否完赛
    if (!is_match_finished($matchStatus)) {
        // 尝试从 score 字符串解析
        if ($scoreStr && preg_match('/(\d+)\s*[-:]\s*(\d+)/', $scoreStr, $m)) {
            $homeScore = (int) $m[1];
            $awayScore = (int) $m[2];
        } else {
            continue; // 未完赛且无比分，跳过
        }
    }

    if ($homeScore === null || $awayScore === null) {
        continue;
    }

    // 结算
    switch ($market) {
        case '1x2':
            [$hitStatus, $resultText] = settle_1x2($row, (int) $homeScore, (int) $awayScore);
            break;
        case 'over_under':
            [$hitStatus, $resultText] = settle_over_under($row, (int) $homeScore, (int) $awayScore);
            break;
        case 'asian_handicap':
            [$hitStatus, $resultText] = settle_asian_handicap($row, (int) $homeScore, (int) $awayScore);
            break;
        default:
            $hitStatus  = 'void';
            $resultText = "未知市场类型: {$market}";
    }

    // 计算盈亏
    $profit = 0;
    $roi    = 0;
    if ($hitStatus === 'hit') {
        $odds   = (float) ($row['odds'] ?? 1.9);
        $profit = $stakeAmt * ($odds - 1);
        $roi    = ($profit / $stakeAmt) * 100;
        $hit++;
    } elseif ($hitStatus === 'miss') {
        $profit = -$stakeAmt;
        $roi    = -100;
        $miss++;
    } else {
        $void++;
    }
    $total++;

    $prefix = $dryRun ? "[预览] " : "";
    $icon   = $hitStatus === 'hit' ? '✓' : ($hitStatus === 'miss' ? '✗' : '~');

    echo "  {$prefix}{$icon} [{$matchId}] {$homeTeam} vs {$awayTeam} | {$market} | {$hitStatus} | {$resultText}\n";

    if (!$dryRun) {
        $stmtResult->bindValue(':pid',    $predId,    SQLITE3_INTEGER);
        $stmtResult->bindValue(':status', $hitStatus, SQLITE3_TEXT);
        $stmtResult->bindValue(':stake',  $stakeAmt,  SQLITE3_FLOAT);
        $stmtResult->bindValue(':profit', $profit,    SQLITE3_FLOAT);
        $stmtResult->bindValue(':roi',    $roi,       SQLITE3_FLOAT);
        $stmtResult->bindValue(':text',   $resultText, SQLITE3_TEXT);
        $stmtResult->execute();
        $stmtResult->reset();

        $stmtUpdatePred->bindValue(':pid', $predId, SQLITE3_INTEGER);
        $stmtUpdatePred->execute();
        $stmtUpdatePred->reset();
    }

    usleep(100000); // 100ms
}

echo "\n──────────────────────────────────────────\n";
echo "  结算结果:\n";
echo "    命中: {$hit} | 未中: {$miss} | 走水: {$void} | 合计: {$total}\n";

if ($total > 0) {
    $hitRate = $hit / max($total - $void, 1) * 100;
    echo "    命中率: " . number_format($hitRate, 1) . "% (不含走水)\n";
}

if ($dryRun) {
    echo "\n  ⚠ 预览模式 — 未写入数据库。去掉 --dry-run 以实际结算。\n";
}

echo "\n══════════════════════════════════════════\n";
echo "  结算完成。\n";
echo "══════════════════════════════════════════\n";