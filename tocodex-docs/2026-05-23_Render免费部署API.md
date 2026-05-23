# Render 免费部署 FastAPI API

## 已完成的部署适配

- `render.yaml`：Render Blueprint 配置，创建免费 Web Service。
- `Dockerfile`：已改为监听 Render 注入的 `PORT` 环境变量。
- `requirements.txt`：补齐 `apscheduler`、`websockets`，避免云端启动失败。
- `.gitignore`：排除本地探测脚本、输出文件、`.env`、SQLite 数据库。

## Render 部署步骤

### 1. 推送代码到 GitHub

如果本地还不是 Git 仓库：

```bash
git init
git add .
git commit -m "deploy api to render"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

### 2. 在 Render 创建 Blueprint

1. 打开 https://dashboard.render.com/
2. New → Blueprint
3. 连接 GitHub 仓库
4. 选择本项目仓库
5. Render 会自动读取根目录 `render.yaml`
6. 点击 Apply / Deploy

### 3. 部署成功后验证

假设 Render 分配域名：

```text
https://football-intel-api.onrender.com
```

打开：

```text
https://football-intel-api.onrender.com/health
https://football-intel-api.onrender.com/docs
https://football-intel-api.onrender.com/match/demo-001/kelly
```

能返回 JSON 即 API 公网可用。

## 注意

- Render 免费实例 15 分钟无请求会休眠，首次唤醒可能需要 30-60 秒。
- 可以用 UptimeRobot 免费每 5 分钟访问 `/health` 保活。
- 如果要关闭 demo 模式，在 Render 环境变量里把 `DEMO_MODE=false`，并填写真实 API Key。
