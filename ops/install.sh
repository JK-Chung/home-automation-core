#!/usr/bin/env bash
# Set up a Raspberry Pi (Raspberry Pi OS / Debian) to run this repo, or re-apply
# it after a `git pull`. Idempotent: safe to run any number of times.
#
#   git clone <repo> /home/raspberrypi/Desktop && cd /home/raspberrypi/Desktop
#   cp .env.example .env && chmod 600 .env    # add NGROK_AUTHTOKEN
#   sudo ./ops/install.sh
set -euo pipefail
OPS="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(dirname "$OPS")"
EXPECTED_REPO=/home/raspberrypi/Desktop      # the systemd units use this path
OWNER="$(stat -c %U "$REPO")"

log() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$REPO" = "$EXPECTED_REPO" ] || { echo "Repo must live at $EXPECTED_REPO (systemd units point there); it is at $REPO" >&2; exit 1; }
[ -s "$REPO/.env" ] || { echo "Missing $REPO/.env - run: cp .env.example .env && chmod 600 .env, then add NGROK_AUTHTOKEN" >&2; exit 1; }
chmod 600 "$REPO/.env"

# --- 1. Docker Engine + compose plugin, from Docker's apt repo -------------------
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  log "Installing Docker"
  . /etc/os-release
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker >/dev/null
id -nG "$OWNER" | grep -qw docker || { usermod -aG docker "$OWNER"; echo "Added $OWNER to the docker group (log out/in to use docker without sudo)"; }

# --- 2. Automatic OS updates ------------------------------------------------------
log "Configuring unattended-upgrades"
dpkg -s unattended-upgrades >/dev/null 2>&1 || apt-get install -y unattended-upgrades
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
  > /etc/apt/apt.conf.d/20auto-upgrades
ln -sfn "$OPS/apt/52unattended-upgrades-local" /etc/apt/apt.conf.d/52unattended-upgrades-local

# --- 3. Notification secrets (never committed) -----------------------------------
if [ ! -f "$OPS/version-watch.env" ]; then
  install -m 600 -o "$OWNER" "$OPS/version-watch.env.example" "$OPS/version-watch.env"
  echo "Created $OPS/version-watch.env - add your HA token, then test with:"
  echo "  sudo python3 $OPS/container-version-watch.py --test"
fi
chmod 600 "$OPS/version-watch.env"

# --- 4. systemd units (linked, so edits in ops/systemd apply after daemon-reload) -
log "Linking systemd units"
for u in "$OPS"/systemd/*.service "$OPS"/systemd/*.timer; do
  systemctl link "$u" >/dev/null
done
systemctl daemon-reload
systemctl enable ha-stack.service container-autoupdate.timer container-version-watch.timer monthly-reboot.timer >/dev/null
systemctl start container-autoupdate.timer container-version-watch.timer monthly-reboot.timer

# --- 5. Retire the old root-crontab jobs (now ha-stack.service / monthly-reboot.timer)
LEGACY='^@reboot .*docker compose|^0 3 1 \* \* reboot'
ROOT_CRON="$(crontab -l -u root 2>/dev/null || true)"
if grep -qE "$LEGACY" <<<"$ROOT_CRON"; then
  log "Removing legacy root crontab entries"
  { grep -vE "$LEGACY" <<<"$ROOT_CRON" || true; } | crontab -u root -
fi

# --- 6. Start the stack (HA trusts ngrok from its first start: ops/ha-trust-ngrok.py)
log "Starting Home Assistant + ngrok (first start pulls ~3 GB of images)"
systemctl restart ha-stack.service   # re-runs `docker compose up -d` (idempotent)
docker compose -f "$REPO/docker-compose.yaml" ps

systemctl list-timers 'container-*' 'monthly-reboot*' --no-pager
