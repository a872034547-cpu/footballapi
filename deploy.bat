@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>nul

if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        if errorlevel 1 (
            echo [ERROR] 无法从 .env.example 复制 .env
            exit /b 1
        )
        echo [INFO] 已创建 .env
    )
)

where docker >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未检测到 Docker，请先安装并确保 docker 命令可用。
    exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 当前 Docker 不支持 "docker compose"，请安装或启用 Docker Compose V2。
    exit /b 1
)

echo [INFO] 正在执行 Docker Compose 部署...
docker compose up -d --build
if errorlevel 1 (
    echo [ERROR] 部署失败，请检查上方日志输出。
    exit /b 1
)

echo [INFO] 部署成功。
echo [INFO] 服务地址: http://localhost:8000
echo [INFO] 健康检查: http://localhost:8000/health

exit /b 0
