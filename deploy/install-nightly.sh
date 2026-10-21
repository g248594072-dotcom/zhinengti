#!/bin/bash
# 每日北京时间02:00运行统一任务，替换该项目原有四条root cron。
set -euo pipefail
GIT_DIR="${GIT_DIR:-/opt/zhinengti}"
if ! docker inspect chat-qc >/dev/null 2>&1; then
  echo "ERROR: chat-qc container missing" >&2
  exit 1
fi
docker exec chat-qc python /app/nightly_pipeline.py --check
mkdir -p /opt/tool-platform/agent-qc/config/schedule-backups
BACKUP="/opt/tool-platform/agent-qc/config/schedule-backups/root-crontab-$(date +%Y%m%d-%H%M%S)"
crontab -l > "$BACKUP" 2>/dev/null || true
python3 - "$BACKUP" "$GIT_DIR" <<'PY'
from pathlib import Path
import subprocess,sys
backup=Path(sys.argv[1])
project=sys.argv[2]
legacy=('fetch_deal_daily.py','fetch_delete_daily.py','daily_job.py','daily_delete_job.py')
lines=backup.read_text().splitlines()
kept=[line for line in lines if not (project in line and any(name in line for name in legacy))]
subprocess.run(['crontab','-'],input='\n'.join(kept)+'\n',text=True,check=True)
print('LEGACY_CRON_REMOVED',len(lines)-len(kept),'BACKUP',backup)
PY
cat > /etc/systemd/system/chat-qc-nightly.service <<'EOF'
[Unit]
Description=SaleSmartly nightly import and independent customer analysis
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec chat-qc python /app/nightly_pipeline.py
TimeoutStartSec=20h
EOF
cat > /etc/systemd/system/chat-qc-nightly.timer <<'EOF'
[Unit]
Description=SaleSmartly nightly pipeline at 02:00 Asia/Shanghai

[Timer]
OnCalendar=*-*-* 02:00:00 Asia/Shanghai
Persistent=true
AccuracySec=1min
Unit=chat-qc-nightly.service

[Install]
WantedBy=timers.target
EOF
systemd-analyze verify /etc/systemd/system/chat-qc-nightly.service /etc/systemd/system/chat-qc-nightly.timer
systemctl daemon-reload
systemctl enable --now chat-qc-nightly.timer
systemctl list-timers chat-qc-nightly.timer --no-pager
echo "NIGHTLY_SCHEDULE_INSTALLED Asia/Shanghai 02:00"
