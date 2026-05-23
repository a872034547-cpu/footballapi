#!/usr/bin/env sh
set -eu

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not in PATH." >&2
  exit 1
fi

docker compose up -d --build

echo "Deployment completed successfully."
echo "Service URL: http://localhost:8000"
echo "Health check: http://localhost:8000/health"
