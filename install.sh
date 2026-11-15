#!/usr/bin/env bash
# Install (or re-apply) this project on a Linux machine. Idempotent: re-run it
# after every `git pull` or after editing anything under host/.
#
#   cp .env.example .env && chmod 600 .env    # add NGROK_AUTHTOKEN first
#   sudo ./install.sh
#
# Works from any directory. Supports apt-based (Debian, Ubuntu, Raspberry Pi OS)
# and dnf-based (Fedora, RHEL and derivatives) systems with systemd.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
OWNER="$(stat -c %U "$PROJECT_DIR")"
UNITS=(ha-stack.service container-autoupdate.service container-autoupdate.timer
       container-version-watch.service container-version-watch.timer
       daily-reboot.service daily-reboot.timer)

log() { printf '\n==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo $0"
command -v systemctl >/dev/null || die "systemd is required"
grep -qE '^NGROK_AUTHTOKEN=.+' "$PROJECT_DIR/.env" 2>/dev/null \
  || die "set NGROK_AUTHTOKEN in $PROJECT_DIR/.env first (cp .env.example .env)"
chmod 600 "$PROJECT_DIR/.env"

if command -v apt-get >/dev/null; then PM=apt
elif command -v dnf >/dev/null; then PM=dnf
else die "unsupported system: needs apt-get or dnf for automatic OS upgrades"; fi

pkg_install() {
  if [ "$PM" = apt ]; then DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
  else dnf install -y "$@"; fi
}

# --- 1. Prerequisites + Docker Engine with the compose plugin -----------------
log "Prerequisites (curl, python3)"
[ "$PM" = apt ] && apt-get update -qq
command -v curl >/dev/null || pkg_install curl
command -v python3 >/dev/null || pkg_install python3

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  log "Installing Docker (official convenience script from get.docker.com)"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi
systemctl enable --now docker >/dev/null
if ! id -nG "$OWNER" | grep -qw docker; then
  usermod -aG docker "$OWNER"
  echo "Added $OWNER to the docker group (log out and back in to use docker without sudo)"
fi

# --- 2. Automatic OS package upgrades -----------------------------------------
log "Automatic OS upgrades ($PM)"
if [ "$PM" = apt ]; then
  dpkg -s unattended-upgrades >/dev/null 2>&1 || pkg_install unattended-upgrades
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    > /etc/apt/apt.conf.d/20auto-upgrades
  install -m 644 "$PROJECT_DIR/host/apt/52unattended-upgrades-local" /etc/apt/apt.conf.d/
  systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null
else
  if command -v dnf5 >/dev/null; then pkg=dnf5-plugin-automatic; timer=dnf5-automatic.timer
  else pkg=dnf-automatic; timer=dnf-automatic.timer; fi
  rpm -q "$pkg" >/dev/null || pkg_install "$pkg"
  conf=/etc/dnf/automatic.conf
  if [ -f "$conf" ] && grep -qE '^\s*apply_updates\s*=' "$conf"; then
    sed -i -E 's/^\s*apply_updates\s*=.*/apply_updates = yes/' "$conf"
  else
    printf '[commands]\napply_updates = yes\n' >> "$conf"
  fi
  systemctl enable --now "$timer" >/dev/null
fi

# --- 3. systemd units (templates: @PROJECT_DIR@ -> this checkout) -------------
log "Installing systemd units for $PROJECT_DIR"
for u in "${UNITS[@]}"; do
  rm -f "/etc/systemd/system/$u"   # replace any older copy or symlink
  tpl="$(cat "$PROJECT_DIR/host/systemd/$u")"
  printf '%s\n' "${tpl//@PROJECT_DIR@/"$PROJECT_DIR"}" > "/etc/systemd/system/$u"
  chmod 644 "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable ha-stack.service container-autoupdate.timer container-version-watch.timer \
  daily-reboot.timer >/dev/null
systemctl restart container-autoupdate.timer container-version-watch.timer daily-reboot.timer

# --- 4. Start (or update) the containers --------------------------------------
log "Starting Home Assistant + ngrok (first run pulls ~3.5 GB of images)"
systemctl restart ha-stack.service   # runs `docker compose up -d` (idempotent)
docker compose --project-directory "$PROJECT_DIR" ps

systemctl list-timers container-autoupdate.timer container-version-watch.timer \
  daily-reboot.timer --no-pager
grep -qE '^HA_TOKEN=.+' "$PROJECT_DIR/.env" || cat <<EOF

Next: open http://<this-machine>:8123, onboard or restore Home Assistant, then add
HA_TOKEN and HA_NOTIFY_SERVICE to $PROJECT_DIR/.env and test reminders with:
  sudo python3 $PROJECT_DIR/host/container-version-watch.py --test
EOF
