<?php
return [
    'api' => [
        'base_url' => 'http://127.0.0.1:8888',
        'timeout' => 30,
    ],
    'db' => [
        'path' => __DIR__ . '/data/predict.db',
    ],
    'predict' => [
        'stake_amount' => 1000,
        'pre_match_hours' => 72,
        'min_confidence' => 50,
    ],
];