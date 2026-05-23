<?php

/**
 * PHP 工具函数库 — 足球预测服务
 *
 * 提供数据库单例、表初始化、API 调用、安全转义、格式化等通用函数。
 */

// ---------------------------------------------------------------------------
// 1. 配置读取
// ---------------------------------------------------------------------------

/**
 * 读取 config.php 中的嵌套配置值（点号分隔）。
 *
 * 示例：
 *   config('api.base_url')   → 'http://127.0.0.1:8888'
 *   config('db.path')        → '/path/to/data/predict.db'
 *   config('predict.stake_amount') → 1000
 *
 * @param  string $key  点号分隔的配置键名
 * @param  mixed  $default  默认值（键不存在时返回）
 * @return mixed
 */
function config(string $key, $default = null)
{
    static $config = null;
    if ($config === null) {
        $config = require __DIR__ . '/config.php';
    }

    $keys  = explode('.', $key);
    $value = $config;

    foreach ($keys as $segment) {
        if (!is_array($value) || !array_key_exists($segment, $value)) {
            return $default;
        }
        $value = $value[$segment];
    }

    return $value;
}

// ---------------------------------------------------------------------------
// 2. 数据库
// ---------------------------------------------------------------------------

/**
 * 返回 SQLite3 单例，数据库路径从 config.php 的 db.path 读取。
 *
 * @return SQLite3
 * @throws RuntimeException 无法打开数据库时抛出
 */
function db(): SQLite3
{
    static $instance = null;
    if ($instance !== null) {
        return $instance;
    }

    $dbPath = config('db.path');
    $dir    = dirname($dbPath);

    if (!is_dir($dir)) {
        if (!mkdir($dir, 0755, true)) {
            throw new RuntimeException("无法创建数据库目录: {$dir}");
        }
    }

    try {
        $instance = new SQLite3($dbPath);
        $instance->enableExceptions(true);
        $instance->exec('PRAGMA journal_mode = WAL');
        $instance->exec('PRAGMA foreign_keys = ON');
    } catch (Exception $e) {
        throw new RuntimeException("无法打开数据库: {$dbPath} — " . $e->getMessage());
    }

    return $instance;
}

/**
 * 初始化数据库表结构。
 *
 * 创建 matches、predictions、prediction_results 三张表（如不存在）。
 *
 * @return void
 */
function init_db(): void
{
    $db = db();

    $db->exec("
        CREATE TABLE IF NOT EXISTS matches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id   TEXT UNIQUE,
            home_team  TEXT,
            away_team  TEXT,
            league     TEXT,
            kickoff    TEXT,
            status     TEXT,
            home_score INTEGER DEFAULT NULL,
            away_score INTEGER DEFAULT NULL,
            minute     INTEGER DEFAULT NULL,
            live_status TEXT DEFAULT NULL,
            score      TEXT,
            raw_json   TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ");

    // 兼容旧表：添加缺失的列
    $alterColumns = [
        'minute'     => 'ALTER TABLE matches ADD COLUMN minute INTEGER DEFAULT NULL',
        'live_status' => 'ALTER TABLE matches ADD COLUMN live_status TEXT DEFAULT NULL',
    ];
    foreach ($alterColumns as $col => $sql) {
        try {
            $db->exec($sql);
        } catch (Exception $e) {
            // 列已存在，忽略
        }
    }

    $db->exec("
        CREATE TABLE IF NOT EXISTS predictions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id        TEXT,
            prediction_type TEXT    DEFAULT 'pre_match',
            market          TEXT,
            direction       TEXT,
            confidence      REAL,
            odds            REAL,
            is_paid         INTEGER DEFAULT 0,
            free_summary    TEXT,
            paid_content    TEXT,
            status          TEXT    DEFAULT 'pending',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ");

    $db->exec("
        CREATE TABLE IF NOT EXISTS prediction_results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER UNIQUE,
            hit_status    TEXT,
            stake         REAL,
            profit        REAL,
            roi           REAL,
            result_text   TEXT,
            settled_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ");
}

// ---------------------------------------------------------------------------
// 3. API 调用
// ---------------------------------------------------------------------------

/**
 * 调用 FastAPI 的 GET 请求，返回解码后的 JSON 数组。
 *
 * @param  string $path  API 路径（如 '/matches/upcoming?date=2026-05-23'）
 * @return array         解码后的 JSON 数组
 * @throws RuntimeException 请求失败或响应非 JSON 时抛出
 */
function api_get(string $path): array
{
    $baseUrl = rtrim(config('api.base_url'), '/');
    $timeout = (int) config('api.timeout', 30);
    $url     = $baseUrl . '/' . ltrim($path, '/');

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_HTTPHEADER     => ['Accept: application/json'],
        CURLOPT_FOLLOWLOCATION => true,
    ]);

    $body       = curl_exec($ch);
    $httpCode   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError  = curl_error($ch);
    curl_close($ch);

    if ($body === false || $curlError !== '') {
        throw new RuntimeException("API 请求失败 [{$url}]: {$curlError}");
    }

    if ($httpCode >= 400) {
        throw new RuntimeException("API 返回 HTTP {$httpCode} [{$url}]: " . substr($body, 0, 500));
    }

    $data = json_decode($body, true);
    if (!is_array($data)) {
        throw new RuntimeException("API 返回非 JSON 数据 [{$url}]");
    }

    return $data;
}

// ---------------------------------------------------------------------------
// 4. 安全转义
// ---------------------------------------------------------------------------

/**
 * HTML 安全转义，防止 XSS。
 *
 * @param  string|null $str
 * @return string
 */
function e(?string $str): string
{
    return htmlspecialchars((string) $str, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

// ---------------------------------------------------------------------------
// 5. 格式化
// ---------------------------------------------------------------------------

/**
 * 金额格式化（保留两位小数，千分位逗号）。
 *
 * @param  float|int $n
 * @return string     如 "1,234.56"
 */
function money_fmt($n): string
{
    return number_format((float) $n, 2, '.', ',');
}

/**
 * 百分比格式化（保留一位小数 + % 符号）。
 *
 * @param  float|int $n  如 0.456 或 45.6
 * @return string        如 "45.6%"
 */
function percent_fmt($n): string
{
    $value = (float) $n;
    // 如果传入的是小数形式（0 < |value| <= 1），自动 ×100
    if ($value > -1 && $value < 1 && $value != 0) {
        $value *= 100;
    }
    return number_format($value, 1, '.', '') . '%';
}