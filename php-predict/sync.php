<?php
/**
 * sync.php — 比赛同步 + 预测生成脚本（CLI）
 *
 * 用法：
 *   php sync.php                  # 同步今天比赛 + 生成预测
 *   php sync.php 2026-05-25       # 同步指定日期
 *   php sync.php --force          # 强制重新分析已有比赛
 *   php sync.php --rolling        # 同步滚球预测（进行中且 minute >= 10）
 *   php sync.php --live           # 刷新所有进行中比赛的实时状态
 *
 * 流程：
 *   1. 从 FastAPI /matches/upcoming 拉取比赛列表
 *   2. 逐场调用 /match/{id}/analysis 获取分析数据
 *   3. 写入 matches 表（upsert）
 *   4. 从分析结果提取 1x2 / over_under / asian_handicap 预测
 *   5. 写入 predictions 表（upsert）
 */

declare(strict_types=1);

require __DIR__ . '/functions.php';

// ---------------------------------------------------------------------------
// 参数解析
// ---------------------------------------------------------------------------
$date    = null;
$force   = false;
$rolling = false;
$live    = false;

foreach ($argv as $i => $arg) {
    if ($i === 0) continue;
    if ($arg === '--force') {
        $force = true;
    } elseif ($arg === '--rolling') {
        $rolling = true;
    } elseif ($arg === '--live') {
        $live = true;
    } elseif (preg_match('/^\d{4}-\d{2}-\d{2}$/', $arg)) {
        $date = $arg;
    }
}

$date = $date ?? date('Y-m-d');

// --rolling / --live 模式：跳过常规同步，执行专项任务
if ($rolling) {
    sync_rolling_predictions();
    exit(0);
}
if ($live) {
    sync_live_status();
    exit(0);
}

$minConfidence = (float) config('predict.min_confidence', 50);

echo "══════════════════════════════════════════\n";
echo "  足球预测同步 — {$date}\n";
echo "  API: " . config('api.base_url') . "\n";
echo "══════════════════════════════════════════\n\n";

// ---------------------------------------------------------------------------
// 1. 初始化数据库
// ---------------------------------------------------------------------------
echo "[1/4] 初始化数据库...\n";
init_db();
echo "  ✓ 数据库就绪\n\n";

// ---------------------------------------------------------------------------
// 2. 拉取比赛列表
// ---------------------------------------------------------------------------
echo "[2/4] 拉取比赛列表 (date={$date})...\n";

try {
    $matches = api_get("matches/upcoming?date={$date}");
} catch (RuntimeException $e) {
    echo "  ✗ API 请求失败: {$e->getMessage()}\n";
    exit(1);
}

if (empty($matches)) {
    echo "  ⚠ 未找到比赛数据（可能当天无比赛或 API 未返回数据）\n";
    exit(0);
}

echo "  ✓ 获取到 " . count($matches) . " 场比赛\n\n";

// ---------------------------------------------------------------------------
// 3. 逐场分析 + 存储
// ---------------------------------------------------------------------------
echo "[3/4] 逐场分析并生成预测...\n";

$db        = db();
$synced    = 0;
$predicted = 0;
$skipped   = 0;
$errors    = 0;

$stmtUpsertMatch = $db->prepare("
    INSERT INTO matches (match_id, home_team, away_team, league, kickoff, status, home_score, away_score, score, raw_json, updated_at)
    VALUES (:mid, :home, :away, :league, :kickoff, :status, :hscore, :ascore, :score, :raw, CURRENT_TIMESTAMP)
    ON CONFLICT(match_id) DO UPDATE SET
        home_team  = excluded.home_team,
        away_team  = excluded.away_team,
        league     = excluded.league,
        kickoff    = excluded.kickoff,
        status     = excluded.status,
        home_score = excluded.home_score,
        away_score = excluded.away_score,
        score      = excluded.score,
        raw_json   = excluded.raw_json,
        updated_at = CURRENT_TIMESTAMP
");

$stmtUpsertPred = $db->prepare("
    INSERT INTO predictions (match_id, prediction_type, market, direction, confidence, odds, free_summary, status, created_at)
    VALUES (:mid, :ptype, :market, :dir, :conf, :odds, :summary, 'pending', CURRENT_TIMESTAMP)
");

$stmtClearPred = $db->prepare("
    DELETE FROM predictions WHERE match_id = :mid AND prediction_type = :ptype
");

foreach ($matches as $m) {
    // API 返回字段: id (非 match_id), home_team/away_team 是嵌套对象
    $matchId   = $m['id'] ?? '';
    $homeTeam  = is_array($m['home_team'] ?? null) ? ($m['home_team']['name'] ?? '') : ($m['home_team'] ?? '');
    $awayTeam  = is_array($m['away_team'] ?? null) ? ($m['away_team']['name'] ?? '') : ($m['away_team'] ?? '');
    $league    = $m['league']     ?? '';
    $kickoff   = $m['kickoff']    ?? '';
    $status    = $m['status']     ?? '';

    if (empty($matchId)) {
        echo "  ⚠ 跳过无效比赛（无 id）\n";
        $skipped++;
        continue;
    }

    // 检查是否已存在且非强制模式
    if (!$force) {
        $existing = $db->querySingle("SELECT 1 FROM matches WHERE match_id = '" . $db->escapeString($matchId) . "'");
        if ($existing) {
            // 检查是否已有预测
            $hasPred = $db->querySingle("SELECT 1 FROM predictions WHERE match_id = '" . $db->escapeString($matchId) . "' LIMIT 1");
            if ($hasPred) {
                $skipped++;
                continue;
            }
        }
    }

    // 存储比赛基本信息
    $stmtUpsertMatch->bindValue(':mid',     $matchId,  SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':home',    $homeTeam, SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':away',    $awayTeam, SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':league',  $league,   SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':kickoff', $kickoff,  SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':status',  $status,   SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':hscore',  $m['home_score'] ?? null, SQLITE3_INTEGER);
    $stmtUpsertMatch->bindValue(':ascore',  $m['away_score'] ?? null, SQLITE3_INTEGER);
    $stmtUpsertMatch->bindValue(':score',   $m['score']      ?? null, SQLITE3_TEXT);
    $stmtUpsertMatch->bindValue(':raw',     json_encode($m, JSON_UNESCAPED_UNICODE), SQLITE3_TEXT);
    $stmtUpsertMatch->execute();
    $stmtUpsertMatch->reset();

    // 调用分析 API
    try {
        $analysis = api_get("match/{$matchId}/analysis");
    } catch (RuntimeException $e) {
        echo "  ✗ 分析失败 [{$matchId}] {$homeTeam} vs {$awayTeam}: {$e->getMessage()}\n";
        $errors++;
        continue;
    }

    // 清除旧预测（force 模式）
    if ($force) {
        $stmtClearPred->bindValue(':mid',   $matchId,    SQLITE3_TEXT);
        $stmtClearPred->bindValue(':ptype', 'pre_match', SQLITE3_TEXT);
        $stmtClearPred->execute();
        $stmtClearPred->reset();
    }

    $matchPredicted = 0;

    // --- 胜平负 (1x2) ---
    // API 返回: {label, probability, rationale}，probability 已是百分比
    $wdl = $analysis['win_draw_lose'] ?? [];
    foreach ($wdl as $p) {
        $conf = (float) ($p['probability'] ?? 0);
        if ($conf < $minConfidence) continue;

        $stmtUpsertPred->bindValue(':mid',     $matchId,       SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':ptype',   'pre_match',    SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':market',  '1x2',          SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':dir',     $p['label']     ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':conf',    $conf,          SQLITE3_FLOAT);
        $stmtUpsertPred->bindValue(':odds',    null,           SQLITE3_NULL);
        $stmtUpsertPred->bindValue(':summary', $p['rationale'] ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->execute();
        $stmtUpsertPred->reset();
        $matchPredicted++;
    }

    // --- 大小球 (over_under) ---
    $ou = $analysis['over_under'] ?? [];
    foreach ($ou as $p) {
        $conf = (float) ($p['probability'] ?? 0);
        if ($conf < $minConfidence) continue;

        $stmtUpsertPred->bindValue(':mid',     $matchId,       SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':ptype',   'pre_match',    SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':market',  'over_under',   SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':dir',     $p['label']     ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':conf',    $conf,          SQLITE3_FLOAT);
        $stmtUpsertPred->bindValue(':odds',    null,           SQLITE3_NULL);
        $stmtUpsertPred->bindValue(':summary', $p['rationale'] ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->execute();
        $stmtUpsertPred->reset();
        $matchPredicted++;
    }

    // --- 亚盘 (asian_handicap) ---
    $ah = $analysis['asian_handicap'] ?? [];
    foreach ($ah as $p) {
        $conf = (float) ($p['probability'] ?? 0);
        if ($conf < $minConfidence) continue;

        $stmtUpsertPred->bindValue(':mid',     $matchId,       SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':ptype',   'pre_match',    SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':market',  'asian_handicap', SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':dir',     $p['label']     ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->bindValue(':conf',    $conf,          SQLITE3_FLOAT);
        $stmtUpsertPred->bindValue(':odds',    null,           SQLITE3_NULL);
        $stmtUpsertPred->bindValue(':summary', $p['rationale'] ?? '', SQLITE3_TEXT);
        $stmtUpsertPred->execute();
        $stmtUpsertPred->reset();
        $matchPredicted++;
    }

    if ($matchPredicted > 0) {
        echo "  ✓ [{$matchId}] {$homeTeam} vs {$awayTeam} → {$matchPredicted} 条预测\n";
        $predicted += $matchPredicted;
    } else {
        echo "  - [{$matchId}] {$homeTeam} vs {$awayTeam} → 无满足置信度阈值的预测\n";
    }

    $synced++;

    // 请求间隔，避免打爆 API
    usleep(200000); // 200ms
}

echo "\n  同步: {$synced} 场 | 预测: {$predicted} 条 | 跳过: {$skipped} | 错误: {$errors}\n\n";

// ---------------------------------------------------------------------------
// 4. 汇总
// ---------------------------------------------------------------------------
echo "[4/4] 汇总统计...\n";

$totalMatches  = $db->querySingle("SELECT COUNT(*) FROM matches");
$totalPreds    = $db->querySingle("SELECT COUNT(*) FROM predictions");
$pendingPreds  = $db->querySingle("SELECT COUNT(*) FROM predictions WHERE status = 'pending'");
$settledPreds  = $db->querySingle("SELECT COUNT(*) FROM predictions WHERE status = 'settled'");

echo "  数据库状态:\n";
echo "    比赛总数: {$totalMatches}\n";
echo "    预测总数: {$totalPreds}\n";
echo "    待结算:   {$pendingPreds}\n";
echo "    已结算:   {$settledPreds}\n";

echo "\n══════════════════════════════════════════\n";
echo "  同步完成。\n";
echo "══════════════════════════════════════════\n";

// ---------------------------------------------------------------------------
// 5. 滚球预测同步 (--rolling)
// ---------------------------------------------------------------------------

/**
 * 同步滚球预测：从 SQLite 读取进行中且 minute >= 10 的比赛，
 * 调用 FastAPI /match/{id}/rolling 获取滚球预测并写入 rolling_predictions 表。
 */
function sync_rolling_predictions(): void
{
    echo "══════════════════════════════════════════\n";
    echo "  滚球预测同步\n";
    echo "  API: " . config('api.base_url') . "\n";
    echo "══════════════════════════════════════════\n\n";

    init_db();
    $db = db();

    // 创建 rolling_predictions 表（如不存在）
    $db->exec("
        CREATE TABLE IF NOT EXISTS rolling_predictions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id         TEXT,
            minute           INTEGER,
            score            TEXT,
            prediction_type  TEXT,
            direction        TEXT,
            probability      REAL,
            rationale        TEXT,
            confidence_level TEXT,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ");

    // 确保 minute 列存在（兼容旧表）
    try {
        $db->exec("ALTER TABLE matches ADD COLUMN minute INTEGER DEFAULT NULL");
    } catch (Exception $e) {
        // 列已存在，忽略
    }

    // 查询进行中且 minute >= 10 的比赛
    $result = $db->query("
        SELECT match_id, home_team, away_team, home_score, away_score,
               COALESCE(minute, 0) AS minute, status
        FROM matches
        WHERE status LIKE '%LIVE%' OR status LIKE '%HT%'
    ");

    $matches = [];
    while ($row = $result->fetchArray(SQLITE3_ASSOC)) {
        $minute = (int) ($row['minute'] ?? 0);
        if ($minute >= 10) {
            $matches[] = $row;
        }
    }

    if (empty($matches)) {
        echo "  ⚠ 没有符合条件的进行中比赛（minute >= 10）\n";
        return;
    }

    echo "  找到 " . count($matches) . " 场符合条件的比赛\n\n";

    $stmtInsert = $db->prepare("
        INSERT INTO rolling_predictions (match_id, minute, score, prediction_type, direction, probability, rationale, confidence_level)
        VALUES (:mid, :minute, :score, :ptype, :dir, :prob, :rationale, :clevel)
    ");

    $synced = 0;
    $errors = 0;

    foreach ($matches as $m) {
        $matchId  = $m['match_id'];
        $homeTeam = $m['home_team'];
        $awayTeam = $m['away_team'];
        $hScore   = (int) ($m['home_score'] ?? 0);
        $aScore   = (int) ($m['away_score'] ?? 0);
        $minute   = (int) ($m['minute'] ?? 0);

        try {
            $predictions = api_get("match/{$matchId}/rolling?minute={$minute}&home_score={$hScore}&away_score={$aScore}");
        } catch (RuntimeException $e) {
            echo "  ✗ 滚球预测失败 [{$matchId}] {$homeTeam} vs {$awayTeam}: {$e->getMessage()}\n";
            $errors++;
            continue;
        }

        $count = 0;
        foreach ($predictions as $p) {
            $stmtInsert->bindValue(':mid',       $matchId,                                SQLITE3_TEXT);
            $stmtInsert->bindValue(':minute',    $minute,                                 SQLITE3_INTEGER);
            $stmtInsert->bindValue(':score',     $p['current_score']     ?? '',           SQLITE3_TEXT);
            $stmtInsert->bindValue(':ptype',     $p['prediction_type']   ?? '',           SQLITE3_TEXT);
            $stmtInsert->bindValue(':dir',       $p['label']             ?? '',           SQLITE3_TEXT);
            $stmtInsert->bindValue(':prob',      (float) ($p['probability'] ?? 0),        SQLITE3_FLOAT);
            $stmtInsert->bindValue(':rationale', $p['rationale']         ?? '',           SQLITE3_TEXT);
            $stmtInsert->bindValue(':clevel',    $p['confidence_level']  ?? 'C',          SQLITE3_TEXT);
            $stmtInsert->execute();
            $stmtInsert->reset();
            $count++;
        }

        echo "  ✓ [{$matchId}] {$homeTeam} vs {$awayTeam} ({$hScore}-{$aScore}, {$minute}') → {$count} 条滚球预测\n";
        $synced++;

        usleep(200000); // 200ms 请求间隔
    }

    echo "\n  同步: {$synced} 场 | 错误: {$errors}\n";
    echo "══════════════════════════════════════════\n";
    echo "  滚球预测同步完成。\n";
    echo "══════════════════════════════════════════\n";
}

// ---------------------------------------------------------------------------
// 6. 实时状态同步 (--live)
// ---------------------------------------------------------------------------

/**
 * 同步实时比赛状态：调用 FastAPI /matches/live 获取进行中比赛的实时比分，
 * 更新 matches 表的 home_score、away_score、minute、status 字段。
 */
function sync_live_status(): void
{
    echo "══════════════════════════════════════════\n";
    echo "  实时状态同步\n";
    echo "  API: " . config('api.base_url') . "\n";
    echo "══════════════════════════════════════════\n\n";

    init_db();
    $db = db();

    // 确保 minute 列存在（兼容旧表）
    try {
        $db->exec("ALTER TABLE matches ADD COLUMN minute INTEGER DEFAULT NULL");
    } catch (Exception $e) {
        // 列已存在，忽略
    }

    // 从 API 获取进行中比赛
    try {
        $liveMatches = api_get("matches/live");
    } catch (RuntimeException $e) {
        echo "  ✗ API 请求失败: {$e->getMessage()}\n";
        exit(1);
    }

    if (empty($liveMatches)) {
        echo "  ⚠ 当前没有进行中的比赛\n";
        return;
    }

    echo "  获取到 " . count($liveMatches) . " 场进行中比赛\n\n";

    $stmtUpdate = $db->prepare("
        UPDATE matches
        SET home_score = :hscore,
            away_score = :ascore,
            minute     = :minute,
            status     = :status,
            updated_at = CURRENT_TIMESTAMP
        WHERE match_id = :mid
    ");

    $updated = 0;
    $errors  = 0;

    foreach ($liveMatches as $m) {
        $matchId  = $m['match_id']  ?? '';
        $homeTeam = $m['home_team'] ?? '';
        $awayTeam = $m['away_team'] ?? '';
        $hScore   = $m['home_score'] ?? 0;
        $aScore   = $m['away_score'] ?? 0;
        $minute   = $m['minute']     ?? 0;
        $status   = $m['status']     ?? 'LIVE';

        if (empty($matchId)) {
            continue;
        }

        $stmtUpdate->bindValue(':mid',    $matchId, SQLITE3_TEXT);
        $stmtUpdate->bindValue(':hscore', $hScore,  SQLITE3_INTEGER);
        $stmtUpdate->bindValue(':ascore', $aScore,  SQLITE3_INTEGER);
        $stmtUpdate->bindValue(':minute', $minute,  SQLITE3_INTEGER);
        $stmtUpdate->bindValue(':status', $status,  SQLITE3_TEXT);
        $stmtUpdate->execute();
        $stmtUpdate->reset();

        echo "  ✓ [{$matchId}] {$homeTeam} vs {$awayTeam} → {$hScore}-{$aScore} ({$minute}') [{$status}]\n";
        $updated++;
    }

    echo "\n  更新: {$updated} 场 | 错误: {$errors}\n";
    echo "══════════════════════════════════════════\n";
    echo "  实时状态同步完成。\n";
    echo "══════════════════════════════════════════\n";
}