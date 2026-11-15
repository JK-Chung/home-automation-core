# Home Assistant + ngrok, self-maintaining

Runs [Home Assistant](https://www.home-assistant.io/) in Docker on any Linux machine (a
Raspberry Pi, a mini PC, a VM) and makes it reachable from the internet through an
[ngrok](https://ngrok.com/) tunnel. The machine then looks after itself:

- **Home Assistant + ngrok** run from `docker-compose.yaml`. On every start, a startup script makes
  Home Assistant trust ngrok as a reverse proxy, so the tunnel works.
- **OS packages upgrade automatically**: `unattended-upgrades` on apt systems, `dnf-automatic`
  on dnf systems.
- **Container images upgrade automatically** to the latest *patch/minor* version of their pinned
  major version, weekly.
- **Major-version reminders**: if a container has been on an older major version for 6 months, you
  get a push notification on your phone (via the Home Assistant app) naming the container, then
  another every 2 weeks until you upgrade it.
- **Daily reboot at 03:00** so kernel and library updates take effect.
- **Backups are not part of this project.** Configure Home Assistant's own automatic backups
  (see [Backups](#backups)).

## Repository layout

```
.
├── install.sh                 Installs/updates everything on the host. Run with sudo; idempotent.
├── docker-compose.yaml        The two containers, pinned to a major version each.
├── .env.example               Template for ALL credentials → copy to .env (never committed).
├── AGENTS.md                  Rules and pitfalls for AI coding agents working on this repo.
│
├── homeassistant/             Code that runs INSIDE the Home Assistant container
│   ├── trust-ngrok-proxy.py   Runs before HA on every container start; adds ngrok to trusted proxies.
│   └── test_trust_ngrok_proxy.py
│
├── host/                      Things install.sh puts on the host OS
│   ├── container-version-watch.py   Daily check that sends the major-version reminders.
│   ├── apt/52unattended-upgrades-local   Which repos apt auto-upgrades (apt systems only).
│   └── systemd/               Unit templates; install.sh fills in @PROJECT_DIR@ and installs them:
│       ├── ha-stack.service                 docker compose up -d at boot
│       ├── container-autoupdate.{service,timer}     weekly image pull (Sun 04:30)
│       ├── container-version-watch.{service,timer}  daily reminder check (09:00)
│       └── daily-reboot.{service,timer}             reboot at 03:00
│
└── data/                      (created at first start, gitignored) Home Assistant's /config:
    └── homeassistant/         configuration.yaml, database, .storage, secrets
```

The checkout can live in any directory. `install.sh` works out its own location, and the
compose file only uses paths relative to itself.

## Credentials

All secrets live in one file, `.env`, in the repo root. It's gitignored and `install.sh` sets
its permissions to `600`.

| Variable | Needed | Where to get it |
|---|---|---|
| `NGROK_AUTHTOKEN` | before install | <https://dashboard.ngrok.com/get-started/your-authtoken> |
| `HA_TOKEN` | after HA is set up | HA → your profile → Security → *Long-lived access tokens* |
| `HA_NOTIFY_SERVICE` | after HA is set up | HA → Developer tools → Actions → search `notify.mobile_app_`; enter it without `notify.` |
| `HA_URL` | optional | defaults to `http://localhost:8123` |

The HA values are only used for upgrade reminders. Everything else works without them.

## Installing on a fresh machine

**Requirements:** a 64-bit Linux with systemd and either `apt` (Debian, Ubuntu, Raspberry Pi OS)
or `dnf` (Fedora, RHEL and derivatives); about 8 GB free disk; internet access. An SSD is
strongly recommended over an SD card (image pulls and database migrations are disk-bound).

```bash
# 1. Get the code
sudo apt-get install -y git        # or: sudo dnf install -y git
git clone <this repo> ~/homeassistant-ngrok
cd ~/homeassistant-ngrok

# 2. Credentials
cp .env.example .env && chmod 600 .env
nano .env                          # set NGROK_AUTHTOKEN

# 3. Install: Docker, automatic OS upgrades, systemd timers, then starts the containers
sudo ./install.sh
```

`install.sh` installs Docker (via Docker's official `get.docker.com` script) if it's missing,
configures automatic OS upgrades, installs the systemd units, and starts the stack. The first run
downloads about 3.5 GB of images.

Then:

4. **Set up Home Assistant** at `http://<machine-ip>:8123`: either onboard a new instance, or
   choose *Restore from backup* on the welcome screen. After restoring, run
   `docker restart homeassistant` so the proxy-trust script runs against the restored config.
5. **Find your public URL**: `curl -s localhost:4040/api/tunnels`, or open
   `http://<machine-ip>:4040`. Free ngrok accounts get one fixed domain.
6. **Enable upgrade reminders**: install the Home Assistant app on your phone and log in, add
   `HA_TOKEN` and `HA_NOTIFY_SERVICE` to `.env`, then test:
   ```bash
   sudo python3 host/container-version-watch.py --test
   ```
7. **Set up backups** (next section).

## Backups

Nothing here backs up Home Assistant, and `data/` is not in git. In Home Assistant, go to
**Settings → System → Backups** and turn on automatic backups to an off-machine location (e.g.
Google Drive, OneDrive or a network share). Store the backup **encryption key** somewhere safe:
without it a backup can't be restored. To rebuild a machine, follow the install steps and restore
the latest backup in step 4.

## How it works

**ngrok → Home Assistant.** ngrok has a fixed IP on a private Docker network (`172.19.0.5`, the
`x-ngrok-ip` anchor in `docker-compose.yaml`). Home Assistant rejects proxied requests
(`400: Bad Request`) unless that IP is a trusted proxy. The container's entrypoint runs
`homeassistant/trust-ngrok-proxy.py` before Home Assistant on every start:

- Home Assistant 2026.8+ stores web-server settings in `data/homeassistant/.storage/http` (editable
  under Settings → System → Network). It imports any `http:` YAML once, then ignores it. The
  script adds ngrok's IP to that stored config if it's missing.
- On a brand-new install (before that import), it adds an `http:` block to `configuration.yaml`.
  Home Assistant imports it on first start, and the script removes the block on the next start.
- It only ever adds, and backs up any file before changing it.
- **If it fails, the container stops before Home Assistant starts** (`restart: always` retries),
  so a broken proxy setup is obvious. See `docker logs homeassistant` for `[trust-ngrok-proxy] ERROR`.

**Container upgrades.** Each image tag pins a major version: `ngrok/ngrok:3` (semver) and
`home-assistant:2026.11` (Home Assistant's monthly release counts as its major, because that's
where breaking changes land). `container-autoupdate.timer` runs `docker compose pull && up -d`
weekly, which picks up patch/minor releases within those tags.

**Major-version reminders.** `container-version-watch.timer` runs daily. For each container it
works out when the first newer major version came out: from GitHub release dates for Home
Assistant, and from the first day a newer tag appears on Docker Hub for ngrok. After 182 days it
sends a push notification titled `Upgrade needed: <container>`, then repeats every 14 days.
State is kept in `/var/lib/container-version-watch/state.json`.

**OS upgrades.** On apt systems, `unattended-upgrades` installs updates from the distro (all
pockets, not just security), the Raspberry Pi repo when present, and Docker's repo, and keeps
locally modified config files instead of stalling. On dnf systems, `dnf-automatic` applies all
updates. The daily 03:00 reboot activates them. A missed reboot is skipped, not caught up at boot.

## Upgrading a container to a new major version

1. Read the release notes in between (for Home Assistant: the *Backward-incompatible changes*
   section of each monthly release), and clear Settings → Repairs.
2. Make sure a recent Home Assistant backup exists (Settings → System → Backups).
3. Pull the new image first, so downtime stays short:
   `docker pull ghcr.io/home-assistant/home-assistant:<YYYY.M>`
4. Change the tag in `docker-compose.yaml`, run `docker compose up -d`, and watch
   `docker logs -f homeassistant` (the first start migrates the database; this is one-way).
5. Check the public URL, logging in from a browser and the phone app, and Settings → Repairs.
6. Commit the tag change.

To roll back, restore the backup taken in step 2 on the old tag.

## Development

- **Changing host behaviour** (timers, update policy, the reminder script): edit files under
  `host/`, then run `sudo ./install.sh` again. Units are copied from the templates on each run,
  so don't edit `/etc/systemd/system/...` directly.
- **Changing the containers**: edit `docker-compose.yaml`, then `docker compose up -d`.
- **Adding a container to the reminders**: add it to `CONTAINERS` in
  `host/container-version-watch.py` with `kind` `semver_major` (plus `hub_repo`) or
  `ha_monthly`, and pin its tag to a major version.
- **Tests** for the proxy script (no dependencies beyond Python 3):
  ```bash
  python3 -m unittest discover homeassistant -v
  ```
- **Trying things out:**
  ```bash
  sudo python3 host/container-version-watch.py --dry-run   # what would be sent, sends nothing
  sudo python3 host/container-version-watch.py --test      # send a test push
  systemctl list-timers container-autoupdate.timer container-version-watch.timer daily-reboot.timer
  ```

### Logs

| What | Command |
|---|---|
| Home Assistant (incl. proxy script) | `docker logs homeassistant` |
| ngrok | `docker logs ngrok` |
| Weekly image updates | `journalctl -u container-autoupdate` |
| Reminders | `journalctl -u container-version-watch` |
| OS upgrades (apt) | `/var/log/unattended-upgrades/unattended-upgrades.log` |
| OS upgrades (dnf) | `journalctl -u dnf5-automatic` (Fedora) or `-u dnf-automatic` |

## Troubleshooting

- **Public URL shows `400: Bad Request`**: Home Assistant doesn't trust ngrok. Check
  `docker logs homeassistant | grep trust-ngrok-proxy`, and that ngrok's IP in
  `docker-compose.yaml` matches Settings → System → Network → trusted proxies.
- **`homeassistant` keeps restarting**: the proxy script failed. Its `ERROR` line in
  `docker logs homeassistant` says why (e.g. `http: !include …`, or an unknown `.storage/http`
  format after a Home Assistant update, which means this script needs updating).
- **No reminders**: run `sudo python3 host/container-version-watch.py --test`. An HTTP 401
  error means `HA_TOKEN` is invalid. A restored backup brings its own users, so create a new token.
