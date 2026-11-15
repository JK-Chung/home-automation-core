# Raspberry Pi Home Assistant

Home Assistant in Docker on a Raspberry Pi, exposed to the internet through an ngrok tunnel,
plus the host config that keeps it updated and survives reboots.

## Layout

```
docker-compose.yaml              Home Assistant + ngrok
.env.example                     copy to .env (chmod 600) and fill in NGROK_AUTHTOKEN
ops/
  install.sh                     sets up a fresh Pi (Docker, apt, systemd, starts stack); re-run after every pull
  ha-trust-ngrok.py              runs in the HA container on every start: appends ngrok to trusted_proxies
  container-version-watch.py     alerts when a container falls behind its newest major version
  version-watch.env.example      copy to version-watch.env (chmod 600), add HA token
  apt/52unattended-upgrades-local   extra repos + keep-local-conffile options for unattended-upgrades
  systemd/                       stack start at boot, weekly image update, daily version check,
                                 monthly reboot
```

Never committed: `.env`, `ops/version-watch.env`, `.home-assistant/` (all HA config and data), backups.

## Setup on a fresh Pi

Start from Raspberry Pi OS (64-bit), user `raspberrypi`.

```bash
sudo apt-get install -y git
git clone <this repo> /home/raspberrypi/Desktop && cd /home/raspberrypi/Desktop
cp .env.example .env && chmod 600 .env          # add NGROK_AUTHTOKEN
sudo ./ops/install.sh
```

`install.sh` installs Docker (from Docker's apt repo), enables automatic OS updates, links the
systemd units, and starts the stack. HA trusts ngrok from its very first start. Re-run it after
any `git pull`; it's idempotent.

Then put the HA token in `ops/version-watch.env` (created by install.sh) and test with
`sudo python3 ops/container-version-watch.py --test`.

Home Assistant itself (users, integrations, automations, history) is not in git: onboard fresh, or
restore an HA backup. After restoring a backup, run `docker restart homeassistant` so the
container re-applies the ngrok proxy trust to the restored config.

## How it works

**Reboots.** Both containers use `restart: always`, so Docker brings them back on boot.
`ha-stack.service` also runs `docker compose up -d` at boot (a no-op if they're already up;
it's also what starts the stack on a fresh Pi). `monthly-reboot.timer` reboots the Pi at 03:00
on the 1st so kernel/library updates take effect; a missed run is skipped, not caught up.

**ngrok → Home Assistant.** ngrok has a fixed IP (`172.19.0.5`, the `x-ngrok-ip` anchor in
`docker-compose.yaml`), and HA's `configuration.yaml` must trust it as a reverse proxy, or HA
answers every tunnel request with `400 Bad Request`. On every container start, before HA runs,
`ops/ha-trust-ngrok.py` appends that IP to the trusted proxies (and sets
`use_x_forwarded_for: true`) unless it's already covered. It only adds, never removes; it backs
up the file before any change and logs to `docker logs homeassistant` as `[ha-trust-ngrok]`.

Since 2026.8, HA keeps web-server settings in `.storage/http` (Settings → System → Network):
it imports the `http:` YAML once, then ignores it. So the script edits `.storage/http` once that
import has happened, and `configuration.yaml` before it (a brand-new install, where it first
has HA generate its default config; HA then imports the block and the script removes it).
**If the script fails, the container stops before HA starts** (and `restart: always` keeps
retrying), e.g. when `http:` is an `!include` it can't edit. Check `docker logs homeassistant`
for `[ha-trust-ngrok] ERROR`, fix `configuration.yaml`, and it will start on the next retry.

**Volumes use `./` paths.** Compose resolves them relative to this file. Don't use `${PWD}`:
when cron ran compose from `/root`, HA started on an empty config with no proxy settings.

**Container updates** (`container-autoupdate.timer`, Sundays 04:30): `docker compose pull` +
`up -d` + `image prune`. Tags are pinned to a major version, so this only brings patch/minor
updates:

| Image | Tag | Auto-updates | "Major" = | Alert after |
|---|---|---|---|---|
| Home Assistant | `2026.11` | patches (2026.11.x) | monthly release (YYYY.M) | 6 months |
| ngrok | `3` | minor + patch (3.x.y) | semver major | 2 months |

**Major-version alerts** (`container-version-watch.timer`, daily 09:00): if a newer major has been
out longer than the limit, sends a push via the HA mobile app plus a dashboard notification,
repeating weekly until fixed. Run `sudo python3 ops/container-version-watch.py --dry-run` to see
status, `--test` to send a test notification. Logs: `journalctl -u container-version-watch`.

**OS updates.** `unattended-upgrades` runs daily for Debian, Raspberry Pi, Docker and ngrok repos.
Locally modified config files are kept rather than blocking the upgrade.

## Moving Home Assistant to a new monthly release

1. Read the "Backward-incompatible changes" of each release note between your version and the
   target, and clear Settings → Repairs first.
2. Pull the new image while the old one keeps running:
   `docker pull ghcr.io/home-assistant/home-assistant:<YYYY.M>`
3. Cold backup (the database migration is one-way, so this is your rollback):
   ```bash
   docker compose stop homeassistant
   sudo tar czf ~/ha-archive/ha-config-<old-version>-$(date +%F).tgz -C .home-assistant config
   ```
4. Change the tag in `docker-compose.yaml`, then `docker compose up -d homeassistant` and watch
   `docker logs -f homeassistant` (first start migrates the database).
5. Check: tunnel URL loads and you can log in (browser + phone app), Settings → Repairs, devices.
6. Commit the tag change.

Rollback: `docker compose stop homeassistant`, move `.home-assistant/config` aside, extract the
tarball back into `.home-assistant/`, restore the old tag, `docker compose up -d homeassistant`.

## Editing ops files

The files in `/etc` are symlinks into `ops/`, so edit here. After changing a unit file:
`sudo systemctl daemon-reload`. apt config changes apply on the next run.
