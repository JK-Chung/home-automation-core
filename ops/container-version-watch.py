#!/usr/bin/env python3
"""Alert when a container has not been on its newest *major* version for too long.

"Major" per container:
  homeassistant  - the monthly release (YYYY.M). Pinned tag e.g. 2026.3 gets
                   patches automatically; moving to a newer month is manual.
  ngrok          - semver major (the `3` tag).

The clock starts when the FIRST newer major was released (HA: GitHub release
date of YYYY.M.0; ngrok: the day this script first saw a new major tag on
Docker Hub). While overdue, it re-notifies every RENOTIFY_DAYS.

Config: ops/version-watch.env (next to this script)   State: /var/lib/container-version-watch/state.json
Run with --test to send a test notification, --dry-run to print without notifying.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

STATE_FILE = "/var/lib/container-version-watch/state.json"
# Secrets live next to this script (gitignored): ops/version-watch.env
ENV_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "version-watch.env")
RENOTIFY_DAYS = 7
UA = {"User-Agent": "container-version-watch/1.0"}

CONTAINERS = {
    "homeassistant": {"kind": "ha_monthly", "max_days": 182},  # ~6 months
    "ngrok": {"kind": "semver_major", "hub_repo": "ngrok/ngrok", "max_days": 61},  # ~2 months
}

NOW = dt.datetime.now(dt.timezone.utc)


def load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def http_json(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def image_tag(container):
    ref = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.Config.Image}}", container], text=True).strip()
    return ref.rsplit(":", 1)[1] if ":" in ref.split("/")[-1] else "latest"


# ---------- Home Assistant: monthly releases from GitHub ----------
def ha_check(name, cfg, state):
    tag = image_tag(name)
    m = re.fullmatch(r"(\d{4})\.(\d+)", tag)
    if not m:
        return None, f"{name}: image tag '{tag}' is not a YYYY.M pin; cannot judge major version"
    current = (int(m[1]), int(m[2]))

    first_release = {}  # (year, month) -> date of first stable release in that month series
    for page in range(1, 4):
        rels = http_json(f"https://api.github.com/repos/home-assistant/core/releases?per_page=100&page={page}")
        if not rels:
            break
        for r in rels:
            mm = re.fullmatch(r"(\d{4})\.(\d+)\.(\d+)", r["tag_name"])
            if r["prerelease"] or r["draft"] or not mm:
                continue
            key = (int(mm[1]), int(mm[2]))
            when = dt.datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            if key not in first_release or when < first_release[key]:
                first_release[key] = when
    newer = {k: v for k, v in first_release.items() if k > current}
    if not newer:
        return None, f"{name}: {tag} is the newest monthly release"
    latest = max(newer)
    behind_since = min(newer.values())
    return (behind_since, f"{tag}", f"{latest[0]}.{latest[1]}", len(newer)), None


# ---------- semver major via Docker Hub tags (e.g. ngrok/ngrok:3 -> look for :4) ----------
def semver_check(name, cfg, state):
    tag = image_tag(name)
    m = re.match(r"(\d+)", tag)
    if not m:
        return None, f"{name}: image tag '{tag}' is not a major pin; cannot judge major version"
    current = int(m[1])
    newest = current
    while True:
        try:
            http_json(f"https://hub.docker.com/v2/repositories/{cfg['hub_repo']}/tags/{newest + 1}")
            newest += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            raise
    seen = state.setdefault("first_seen", {}).setdefault(name, {})
    if newest == current:
        seen.clear()
        return None, f"{name}: major {current} is the newest"
    first = seen.setdefault(str(current + 1), NOW.isoformat())
    return (dt.datetime.fromisoformat(first), tag, str(newest), newest - current), None


CHECKS = {"ha_monthly": ha_check, "semver_major": semver_check}


# ---------- notification ----------
def notify(env, title, message):
    method = env.get("NOTIFY_METHOD", "log")
    if method == "ha":
        base = env.get("HA_URL", "http://localhost:8123").rstrip("/")
        hdr = {"Authorization": f"Bearer {env['HA_TOKEN']}", "Content-Type": "application/json"}
        body = json.dumps({"title": title, "message": message}).encode()
        http_json(f"{base}/api/services/notify/{env['HA_NOTIFY_SERVICE']}", hdr, body, "POST")
        http_json(f"{base}/api/services/persistent_notification/create", hdr, body, "POST")
    print(f"NOTIFY[{method}] {title}: {message}")


def main():
    dry = "--dry-run" in sys.argv
    env = load_env()
    if "--test" in sys.argv:
        notify(env, "Raspberry Pi version watch", "Test notification: alerts are working.")
        return 0
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    last = state.setdefault("last_notified", {})
    rc = 0
    for name, cfg in CONTAINERS.items():
        try:
            result, info = CHECKS[cfg["kind"]](name, cfg, state)
        except Exception as e:  # keep checking the other containers
            print(f"{name}: check failed: {e!r}", file=sys.stderr)
            rc = 1
            continue
        if result is None:
            print(info)
            last.pop(name, None)
            continue
        behind_since, cur, newest, count = result
        days = (NOW - behind_since).days
        print(f"{name}: on {cur}, newest major {newest} ({count} newer), behind for {days}d "
              f"(limit {cfg['max_days']}d)")
        if days < cfg["max_days"]:
            continue
        prev = last.get(name)
        if prev and (NOW - dt.datetime.fromisoformat(prev)).days < RENOTIFY_DAYS:
            continue
        msg = (f"{name} is on {cur} but {newest} is out ({count} major release(s) newer). "
               f"It has not been on the latest major for {days} days (limit {cfg['max_days']}). "
               f"Update the image tag in ~/Desktop/docker-compose.yaml after reading the release notes.")
        if dry:
            print("DRY-RUN would notify:", msg)
        else:
            notify(env, f"Update needed: {name}", msg)
            last[name] = NOW.isoformat()
    if not dry:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
