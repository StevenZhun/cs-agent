#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_root"

if [[ ! -x .venv/bin/python || ! -x .venv/bin/alembic ]]; then
  echo "缺少项目虚拟环境，请先在项目根目录运行 uv sync --frozen" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "缺少根目录 .env，请复制 .env.example 并填写本地配置" >&2
  exit 1
fi

compose=(docker compose --env-file .env -f infra/docker/compose.yaml)

mkdir -p .runtime/tls
if [[ ! -f .runtime/tls/localhost.crt || ! -f .runtime/tls/localhost.key ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout .runtime/tls/localhost.key -out .runtime/tls/localhost.crt \
    -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1
  chmod 600 .runtime/tls/localhost.key
fi

"${compose[@]}" up -d postgres redis etcd minio milvus
.venv/bin/python -m scripts.bootstrap_all

npm --prefix frontend/workbench ci
npm --prefix frontend/workbench run build

"${compose[@]}" --profile app up -d --build --force-recreate

echo "服务已启动：http://localhost:8080（自动跳转 HTTPS）"
echo "公开仓库不附带预置客户与登录绑定；导入业务数据并创建账号后方可登录。"
