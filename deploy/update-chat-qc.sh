#!/bin/bash
# 在服务器上执行：将 /opt/zhinengti 同步到 agent-qc 并重建 chat-qc 容器
set -euo pipefail

GIT_DIR="${GIT_DIR:-/opt/zhinengti}"
BUILD_DIR="${BUILD_DIR:-/opt/tool-platform/agent-qc}"
PLATFORM_DIR="${PLATFORM_DIR:-/opt/tool-platform}"

echo "==> git pull"
cd "$GIT_DIR"
git pull origin main

echo "==> rsync code (保留 config/ 与 Dockerfile)"
rsync -av --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'config/' \
  --exclude 'qc_config.json' \
  --exclude '.env' \
  --exclude 'api-key.json' \
  "$GIT_DIR/智能体/" "$BUILD_DIR/"

cp -f "$GIT_DIR/deploy/agent-qc/Dockerfile" "$BUILD_DIR/Dockerfile"
cp -f "$GIT_DIR/deploy/agent-qc/.dockerignore" "$BUILD_DIR/.dockerignore"

echo "==> verify delete-customer modules"
for f in db.py delete_import_core.py delete_intelligence.py delete_context.py \
         fetch_delete_daily.py daily_delete_job.py; do
  if [ ! -f "$BUILD_DIR/$f" ]; then
    echo "ERROR: missing $f after rsync" >&2
    exit 1
  fi
done
if ! grep -q 'def mark_sessions_as_deleted' "$BUILD_DIR/db.py"; then
  echo "ERROR: db.py on server is outdated (missing mark_sessions_as_deleted)." >&2
  echo "       Run: cd $GIT_DIR && git pull origin main" >&2
  exit 1
fi

if ! grep -q 'DELETE_SUPPLEMENT_FILE' "$BUILD_DIR/weekly_prompt_merge.py"; then
  echo "ERROR: weekly_prompt_merge.py is outdated (missing DELETE_SUPPLEMENT_FILE)." >&2
  echo "       Run: cd $GIT_DIR && git pull origin main" >&2
  exit 1
fi

mkdir -p "$BUILD_DIR/config"
for f in qc_config.json .env api-key.json import_deal_config.json; do
  if [ -f "$GIT_DIR/智能体/$f" ]; then
    cp -f "$GIT_DIR/智能体/$f" "$BUILD_DIR/config/"
  fi
done
if [ ! -f "$BUILD_DIR/config/qc_config.json" ] && [ -f "$BUILD_DIR/config/qc_config.example.json" ]; then
  cp "$BUILD_DIR/config/qc_config.example.json" "$BUILD_DIR/config/qc_config.json"
fi

echo "==> docker compose build & up"
cd "$PLATFORM_DIR"
docker compose build chat-qc
docker compose up -d chat-qc

echo "==> done"
docker ps --filter name=chat-qc
docker logs chat-qc --tail 15
