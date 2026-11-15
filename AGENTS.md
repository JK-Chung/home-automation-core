# AGENTS.md

Guidance for AI coding agents working on this repository. Read `README.md` first for what the
project does and how it's installed; this file covers the rules and pitfalls for changing it.

## What this is

Home Assistant + ngrok in Docker on a Linux host, plus host automation: OS auto-upgrades, weekly
patch/minor image updates, push reminders when a container falls behind a major version, and a
daily 03:00 reboot. No build step; it's shell, Python (stdlib only) and systemd units.

## Layout

| Path | Runs where | Notes |
|---|---|---|
| `install.sh` | host, as root | Single entry point. Must stay idempotent; re-run after any `host/` change |
| `docker-compose.yaml` | Docker | ngrok IP is the `x-ngrok-ip` anchor, used in two places |
| `homeassistant/trust-ngrok-proxy.py` | inside the HA container, before HA starts | Mounted read-only; non-zero exit stops the container |
| `host/container-version-watch.py` | host, root, daily timer | Major-version reminders; config in `CONTAINERS` |
| `host/systemd/*` | templates | `@PROJECT_DIR@` is substituted by `install.sh`; never hard-code paths |
| `host/apt/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/` | apt systems only; dnf is configured inline in `install.sh` |
| `data/` | runtime | HA's `/config`. Gitignored. Never read secrets out of it into code or commits |
| `.env` | host | All credentials. Gitignored. Only `.env.example` is committed |

## Rules

- **Path-agnostic:** the checkout can live anywhere. Use paths relative to the compose file, or
  `@PROJECT_DIR@` in units. Never use `${PWD}` in `docker-compose.yaml`: a cron job once ran
  compose from `/root`, and HA came up on an empty config, which broke the tunnel.
- **Distro-agnostic:** support apt and dnf systems with systemd. Raspberry Pi specifics must be
  optional no-ops elsewhere.
- **Credentials only in `.env`:** add new ones to `.env.example` (with where to get them) and to
  the README's Credentials table. Never print secret values in logs or command output.
- **Stdlib only:** Python must run on a bare host and inside the HA image without pip installs.
  Shell uses `set -euo pipefail`.
- **Backups are out of scope:** don't add backup logic; Home Assistant's own automatic backups
  are the documented approach.
- **The proxy script only adds, and fails loudly:** it may append ngrok to trusted proxies and
  set `use_x_forwarded_for`; it must not remove other settings. Any unexpected state → exit 1.
  It backs up a file before changing it and writes atomically.

## Home Assistant facts that matter here

- Since 2026.8, web-server settings live in `data/homeassistant/.storage/http` (format
  version 3: `stable`, `pending`, `yaml_migration_done`). HA imports `http:` YAML **once**, then
  ignores it and raises a repair if it's still present. Edit the store, not the YAML, once
  `yaml_migration_done` is true. If HA changes the store version, update `STORE_VERSION` and
  `patch_store()` and add a test.
- The entrypoint `python3 … && exec /init` keeps s6's `/init` as PID 1. Don't change it to `;`
  (that would start HA after a failure) or drop `exec`.
- Restoring an HA backup happens inside the running container, so the proxy script doesn't run
  until `docker restart homeassistant`.
- HA "major" = monthly release `YYYY.M`; breaking changes land monthly. ngrok uses semver.

## Commands

```bash
python3 -m unittest discover homeassistant -v                # proxy script tests (temp dirs, no Docker)
bash -n install.sh                                           # syntax check
NGROK_AUTHTOKEN=x docker compose config --quiet             # validate compose
sudo python3 host/container-version-watch.py --dry-run      # reminder status, sends nothing
sudo ./install.sh                                            # apply host changes (on a real host)
```

Add or update a test in `homeassistant/test_trust_ngrok_proxy.py` for any behaviour change to the
proxy script. When testing template rendering, use a path with spaces, `&` and `|`. In bash 5.2+,
`&` in a `${var//pat/rep}` replacement expands to the match unless `rep` is quoted.

## Working on a live host

- This usually runs a real home's Home Assistant. Ask before restarting containers, rebooting,
  running `install.sh`, or anything that causes downtime; say how long it will take.
- The host reboots daily at 03:00. Don't schedule long jobs across it.
- On SD-card hosts, image pulls and DB migrations are slow (minutes, high load). Pull an image
  before stopping the old container.
- When sending files over `ssh`, copy them with `scp` rather than an inline heredoc inside a
  single-quoted command. An apostrophe in the content ends the quoting early and truncates the
  file.
- Don't commit or push unless asked. Never commit `.env`, `data/`, or anything from `ha-archive`.
