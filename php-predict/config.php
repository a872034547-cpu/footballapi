<?php
return [
    'api' => [
        'base_url' => 'https://football-intel-api.onrender.com',
        'timeout' => 45,  // Render 免费层冷启动可能需 ~30s
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