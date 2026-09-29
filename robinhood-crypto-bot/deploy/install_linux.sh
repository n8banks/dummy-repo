#!/usr/bin/env bash
# One-time setup on a Linux machine (Ubuntu/Debian). Run from the repo root:
#   sudo ./deploy/install_linux.sh
set -euo pipefail

apt-get update
apt-get install -y python3 python3-venv chrony   # chrony keeps the clock right;
                                                 # Robinhood rejects signatures >30s off
id rhbot &>/dev/null || useradd --system --home /var/lib/rhbot --shell /usr/sbin/nologin rhbot
mkdir -p /opt/rhbot /var/lib/rhbot
cp -r rhbot pyproject.toml requirements.txt /opt/rhbot/
python3 -m venv /opt/rhbot/.venv
/opt/rhbot/.venv/bin/pip install -q -r /opt/rhbot/requirements.txt
chown -R rhbot:rhbot /var/lib/rhbot

if [ ! -f /etc/rhbot.env ]; then
  cat > /etc/rhbot.env <<'EOF'
RH_API_KEY=
RH_PRIVATE_KEY=
# RHBOT_LIVE_ACK=I understand this trades real money
# RHBOT_NTFY_TOPIC=pick-a-long-random-name  (phone alerts via the ntfy app)
EOF
  chmod 600 /etc/rhbot.env
  echo "Fill in /etc/rhbot.env, then: systemctl enable --now rhbot"
fi

cp deploy/rhbot.service /etc/systemd/system/rhbot.service
systemctl daemon-reload
echo "Logs: journalctl -u rhbot -f"
