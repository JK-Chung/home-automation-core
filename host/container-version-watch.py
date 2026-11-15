#!/usr/bin/env python3
"""Push a reminder when a container has not been on its newest *major* version
for MAX_BEHIND_DAYS, then every RENOTIFY_DAYS until it is upgraded.

Patch/minor updates are automatic (container-autoupdate.timer pulls the pinned
tag weekly); moving to a new major is a deliberate, manual tag change in
docker-compose.yaml. This script only tells you when that is overdue.

What counts as "major" per container (see CONTAINERS):
  ha_monthly    Home Assistant's monthly release, YYYY.M (tag e.g. 2026.11).
                The clock starts at the GitHub release date of the first
                monthly release newer than the pinned one.
  semver_major  Semantic-version major (tag e.g. `3`). The clock starts on the
                day this script first sees a newer major tag on Docker Hub.

Runs daily from container-version-watch.timer. Sends a push notification via
Home Assistant (mobile app) plus a dashboard notification. Credentials come
from the repo's .env: HA_TOKEN, HA_NOTIFY_SERVICE, optional HA_URL.

  sudo python3 host/container-version-watch.py --dry-run   # status, no notification
  sudo python3 host/container-version-watch.py --test      # send a test notification
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV_FILE = os.path.join(REPO, ".env")
STATE_FILE = "/var/lib/container-version-watch/state.json"
MAX_BEHIND_DAYS = 182   # ~6 months on a non-latest major before the first reminder
RENOTIFY_DAYS = 14      # then a reminder every 2 weeks until upgraded
UA = {"User-Agent": "container-version-watch/2.0"}

# container_name -> how to judge its major version
CONTAINERS = {
    "homeassistant": {"kind": "ha_monthly"},
    "ngrok": {"kind": "semver_major", "hub_repo": "ngrok/ngrok"},
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


def ha_monthly(name, cfg, state):
    """Return (behind_since, current, newest, count_newer) or None if up to date."""
    tag = image_tag(name)
    m = re.fullmatch(r"(\d{4})\.(\d+)", tag)
    if not m:
        raise ValueError(f"image tag '{tag}' is not a YYYY.M pin")
    current = (int(m[1]), int(m[2]))
    first_release = {}  # (year, month) -> first stable release date of that series
    for page in range(1, 4):
        rels = http_json("https://api.github.com/repos/home-assistant/core/releases"
                         f"?per_page=100&page={page}")
        if not rels:
            break
        for r in rels:
            mm = re.fullmatch(r"(\d{4})\.(\d+)\.(\d+)", r["tag_name"])
            if r["prerelease"] or r["draft"] or not mm:
                continue
            key = (int(mm[1]), int(mm[2]))
            when = dt.datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            first_release[key] = min(when, first_release.get(key, when))
    newer = {k: v for k, v in first_release.items() if k > current}
    if not newer:
        return None
    latest = max(newer)
    return min(newer.values()), tag, f"{latest[0]}.{latest[1]}", len(newer)


def semver_major(name, cfg, state):
    tag = image_tag(name)
    m = re.match(r"(\d+)", tag)
    if not m:
        raise ValueError(f"image tag '{tag}' is not a major-version pin")
    current = newest = int(m[1])
    while True:  # probe Docker Hub for :<major+1>, :<major+2>, ...
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
        return None
    first = seen.setdefault(str(current + 1), NOW.isoformat())
    return dt.datetime.fromisoformat(first), tag, str(newest), newest - current


CHECKS = {"ha_monthly": ha_monthly, "semver_major": semver_major}


def notify(env, title, message):
    missing = [k for k in ("HA_TOKEN", "HA_NOTIFY_SERVICE") if not env.get(k)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not set in {ENV_FILE}")
    base = env.get("HA_URL", "http://localhost:8123").rstrip("/")
    hdr = {"Authorization": f"Bearer {env['HA_TOKEN']}", "Content-Type": "application/json"}
    body = json.dumps({"title": title, "message": message}).encode()
    service = env["HA_NOTIFY_SERVICE"].removeprefix("notify.")
    http_json(f"{base}/api/services/notify/{service}", hdr, body, "POST")
    http_json(f"{base}/api/services/persistent_notification/create", hdr, body, "POST")
    print(f"NOTIFIED: {title}: {message}")


def main():
    dry = "--dry-run" in sys.argv
    env = load_env()
    if "--test" in sys.argv:
        notify(env, "Container version watch", "Test notification: update reminders are working.")
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
            result = CHECKS[cfg["kind"]](name, cfg, state)
        except Exception as e:  # keep checking the other containers
            print(f"{name}: check failed: {e}", file=sys.stderr)
            rc = 1
            continue
        if result is None:
            print(f"{name}: on the newest major version")
            last.pop(name, None)
            continue
        behind_since, cur, newest, count = result
        days = (NOW - behind_since).days
        print(f"{name}: on {cur}, newest major {newest} ({count} newer), "
              f"behind for {days}d (reminder after {MAX_BEHIND_DAYS}d)")
        if days < MAX_BEHIND_DAYS:
            continue
        prev = last.get(name)
        if prev and (NOW - dt.datetime.fromisoformat(prev)).days < RENOTIFY_DAYS:
            continue
        title = f"Upgrade needed: {name}"
        msg = (f"The {name} container is on {cur}, but {newest} is out "
               f"({count} major version(s) newer). It has not been on the latest major "
               f"version for {days} days. Change its image tag in "
               f"{os.path.join(REPO, 'docker-compose.yaml')} after reading the release notes.")
        if dry:
            print(f"DRY-RUN would notify: {title}: {msg}")
            continue
        try:
            notify(env, title, msg)
            last[name] = NOW.isoformat()
        except Exception as e:
            print(f"{name}: notification failed: {e}", file=sys.stderr)
            rc = 1
    if not dry:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
