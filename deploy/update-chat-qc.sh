#!/bin/bash
# 在服务器上执行：将 /opt/zhinengti 同步到 agent-qc 并重建 chat-qc 容器
set -euo pipefail

GIT_DIR="${GIT_DIR:-/opt/zhinengti}"
BUILD_DIR="${BUILD_DIR:-/opt/tool-platform/agent-qc}"
PLATFORM_DIR="${PLATFORM_DIR:-/opt/tool-platform}"

echo "==> git pull"
cd "$GIT_DIR"
git pull origin main

# 网页在线审核写入的规则属于运行数据，先从旧容器备份，避免重建镜像覆盖。
KNOWLEDGE_BACKUP_DIR=""
if docker inspect chat-qc >/dev/null 2>&1; then
  KNOWLEDGE_BACKUP_DIR="$BUILD_DIR/config/knowledge-backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$KNOWLEDGE_BACKUP_DIR"
  for f in deal_learned_supplement.md delete_learned_supplement.md; do
    docker cp "chat-qc:/app/prompts/$f" "$KNOWLEDGE_BACKUP_DIR/$f"
  done
fi

echo "==> rsync code (保留 config/ 与 Dockerfile)"
rsync -av --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.venv/' \
  --exclude 'config/' \
  --exclude 'qc_config.json' \
  --include '.env.example' \
  --exclude '.env*' \
  --exclude '*.bak' \
  --exclude '*.save' \
  --exclude 'qc_config.json.*' \
  --exclude 'api-key.json.*' \
  --exclude 'import_deal_config.json.*' \
  --exclude 'api-key.json' \
  --exclude 'import_deal_config.json' \
  "$GIT_DIR/智能体/" "$BUILD_DIR/"

if [ -n "$KNOWLEDGE_BACKUP_DIR" ]; then
  mkdir -p "$BUILD_DIR/prompts"
  for f in deal_learned_supplement.md delete_learned_supplement.md; do
    cp -f "$KNOWLEDGE_BACKUP_DIR/$f" "$BUILD_DIR/prompts/$f"
  done
fi

cp -f "$GIT_DIR/deploy/agent-qc/Dockerfile" "$BUILD_DIR/Dockerfile"
cp -f "$GIT_DIR/deploy/agent-qc/.dockerignore" "$BUILD_DIR/.dockerignore"

echo "==> verify delete-customer modules"
for f in db.py delete_import_core.py delete_intelligence.py delete_context.py \
         fetch_delete_daily.py fetch_delete_salesmartly.py daily_delete_job.py; do
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
  if [ ! -f "$BUILD_DIR/config/$f" ] && [ -f "$GIT_DIR/智能体/$f" ]; then
    cp -f "$GIT_DIR/智能体/$f" "$BUILD_DIR/config/"
  fi
done
if [ ! -f "$BUILD_DIR/config/qc_config.json" ] && [ -f "$BUILD_DIR/config/qc_config.example.json" ]; then
  cp "$BUILD_DIR/config/qc_config.example.json" "$BUILD_DIR/config/qc_config.json"
fi

echo "==> 使用现有 config/（数据库地址在 config/.env，不覆盖线上配置）"
echo "==> docker compose build & up"
cd "$PLATFORM_DIR"
docker compose build chat-qc
docker compose up -d chat-qc

echo "==> done"
docker ps --filter name=chat-qc
docker logs chat-qc --tail 15

# 仅做只读连接检查；失败给出配置位置，不自动更改生产数据库地址。
echo "==> MySQL connection check (read-only)"
if ! docker compose exec -T chat-qc python -c 'from db import test_mysql_connection; test_mysql_connection(); print("MySQL connection OK")'; then
  echo "WARNING: 网页已更新，但 MySQL 仍不可达。请检查 $BUILD_DIR/config/.env 及 compose 的 MYSQL_* 覆盖项。" >&2
  echo "参考：$GIT_DIR/deploy/mysql-connection.md" >&2
fi
