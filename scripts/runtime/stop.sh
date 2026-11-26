#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_root"

docker compose --env-file .env -f infra/docker/compose.yaml --profile app stop

echo "应用与基础设施容器已停止，数据卷仍保留。"
