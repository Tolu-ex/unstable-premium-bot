#!/usr/bin/env bash
# Deploys the watcher to any Linux box over SSH and starts it under systemd.
#
#   ./deploy/deploy.sh ubuntu@<server-ip> [path/to/private-key]
#
# Needs nothing on the server but python3 (preinstalled on Oracle's Ubuntu
# images). No inbound ports - the bot only makes outbound HTTPS calls.
set -euo pipefail

TARGET="${1:-}"
KEY="${2:-}"
if [ -z "$TARGET" ]; then
  echo "usage: $0 user@host [ssh-key]" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env found - nothing to deploy." >&2; exit 1; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")

REMOTE_DIR="/home/\$(whoami)/unstable-premium-bot"

echo "==> checking connection to $TARGET"
ssh "${SSH_OPTS[@]}" "$TARGET" 'python3 -V && echo "ok: $(whoami)@$(hostname)"'

echo "==> copying files"
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ~/unstable-premium-bot"
SCP_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SCP_OPTS+=(-i "$KEY")
# systemd reads EnvironmentFile literally - an inline "# comment" would become
# part of the value and break numeric settings. Strip them.
CLEAN_ENV="$(mktemp)"
trap 'rm -f "$CLEAN_ENV"' EXIT
sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//' .env | grep -vE '^[[:space:]]*(#|$)' > "$CLEAN_ENV"
scp "${SCP_OPTS[@]}" -q unstable_alert.py "$TARGET:~/unstable-premium-bot/"
scp "${SCP_OPTS[@]}" -q "$CLEAN_ENV" "$TARGET:~/unstable-premium-bot/.env"
ssh "${SSH_OPTS[@]}" "$TARGET" 'chmod 600 ~/unstable-premium-bot/.env'

echo "==> installing systemd unit"
scp "${SCP_OPTS[@]}" -q deploy/unstable-premiumbot.service "$TARGET:/tmp/unstable-premiumbot.service"
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
DIR="$HOME/unstable-premium-bot"
sed -e "s|__USER__|$(whoami)|g" -e "s|__DIR__|$DIR|g" \
    /tmp/unstable-premiumbot.service | sudo tee /etc/systemd/system/unstable-premiumbot.service >/dev/null
rm -f /tmp/unstable-premiumbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now unstable-premiumbot
sleep 4
sudo systemctl is-active unstable-premiumbot
REMOTE

echo "==> recent log"
ssh "${SSH_OPTS[@]}" "$TARGET" 'tail -n 15 ~/unstable-premium-bot/bot.log 2>/dev/null || journalctl -u unstable-premiumbot -n 15 --no-pager'

cat <<DONE

Deployed. Useful commands on the server:
  sudo systemctl status unstable-premiumbot
  sudo systemctl restart unstable-premiumbot
  tail -f ~/unstable-premium-bot/bot.log

IMPORTANT: stop the copy on your Mac, or you'll get duplicate alerts:
  launchctl unload ~/Library/LaunchAgents/com.unstable.premiumbot.plist
DONE
