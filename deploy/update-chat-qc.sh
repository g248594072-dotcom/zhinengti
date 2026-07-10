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
