<?php
// PHP 内置服务器路由脚本
// 用法: php -S 0.0.0.0:8899 php-predict/router.php

$uri = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$root = __DIR__;  // php-predict 目录

// 静态文件：从 php-predict 目录提供
if (preg_match('/\.(css|js|png|jpg|gif|svg|ico|woff2?)$/', $uri)) {
    $staticFile = $root . $uri;
    if (is_file($staticFile)) {
        $ext = pathinfo($staticFile, PATHINFO_EXTENSION);
        $mimeTypes = [
            'css' => 'text/css',
            'js' => 'application/javascript',
            'png' => 'image/png',
            'jpg' => 'image/jpeg',
            'jpeg' => 'image/jpeg',
            'gif' => 'image/gif',
            'svg' => 'image/svg+xml',
            'ico' => 'image/x-icon',
            'woff' => 'font/woff',
            'woff2' => 'font/woff2',
        ];
        if (isset($mimeTypes[$ext])) {
            header('Content-Type: ' . $mimeTypes[$ext]);
        }
        readfile($staticFile);
        return true;
    }
    return false;
}

// 根路径 → index.php
if ($uri === '/' || $uri === '/index.php') {
    require $root . '/index.php';
    return true;
}

// 显式 .php 文件
$file = $root . $uri;
if (is_file($file) && pathinfo($file, PATHINFO_EXTENSION) === 'php') {
    require $file;
    return true;
}

// 其他路径尝试作为 PHP 文件
$phpFile = $root . $uri . '.php';
if (is_file($phpFile)) {
    require $phpFile;
    return true;
}

// 404
http_response_code(404);
echo 'Not Found';