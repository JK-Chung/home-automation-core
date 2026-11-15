#!/usr/bin/env python3
"""Make Home Assistant trust the ngrok container as a reverse proxy.

Runs inside the homeassistant container on every start, *before* Home Assistant
(see `entrypoint` in docker-compose.yaml). Without this, Home Assistant answers
every request coming through the tunnel with "400: Bad Request".

It only ever adds: ngrok's IP is appended to trusted_proxies, and
use_x_forwarded_for is set to true (HA requires both). Already trusted (the IP,
or a subnet containing it) -> no change.

Where the setting lives depends on Home Assistant's state:
  * HA >= 2026.8 keeps web-server settings in /config/.storage/http. It imports
    the `http:` YAML once (yaml_migration_done) and ignores YAML afterwards.
    After that import this script edits the stored `stable` and `pending`
    configs, and removes the YAML block it added itself (HA flags leftover YAML).
  * Before that import (a fresh install, or an older HA) it appends to the
    `http:` section of configuration.yaml, which HA then imports on this start.

Any failure exits non-zero, which stops the container before HA starts
(docker-compose.yaml runs `this && exec /init`): a broken proxy setup is loud
instead of a silent "400: Bad Request" through the tunnel.

Env: NGROK_PROXY_IP (required), HA_CONFIG_DIR (default /config).
Tests: python3 -m unittest discover homeassistant
"""
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time

CONFIG_DIR = os.environ.get("HA_CONFIG_DIR", "/config")
CONF = os.path.join(CONFIG_DIR, "configuration.yaml")
STORE = os.path.join(CONFIG_DIR, ".storage", "http")
STORE_VERSION = 3  # .storage/http format this script understands; others fail loudly
IP = os.environ.get("NGROK_PROXY_IP", "").strip()
TAG = "added by trust-ngrok-proxy"


def log(msg):
    print(f"[trust-ngrok-proxy] {msg}", flush=True)


def covers(entry):
    """True if a trusted_proxies entry (IP or network) includes ngrok's IP."""
    try:
        return ipaddress.ip_address(IP) in ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return False


def backup(path):
    shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}")


def write_atomic(path, text):
    """Write via a temp file + rename, keeping the original file's permissions."""
    tmp = f"{path}.trust-ngrok-proxy.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


# ---------- HA >= 2026.8: settings stored in .storage/http ----------
def patch_store(store):
    """Ensure ngrok is trusted in the stored stable/pending configs; return changes."""
    if store.get("version") != STORE_VERSION:
        raise ValueError(f".storage/http is version {store.get('version')}, this script "
                         f"understands version {STORE_VERSION}")
    changes = []
    for slot in ("stable", "pending"):
        conf = store["data"].get(slot)
        if conf is None:  # no pending (unconfirmed) config
            continue
        proxies = conf.get("trusted_proxies") or []
        if not any(covers(p) for p in proxies):
            conf["trusted_proxies"] = proxies + [str(ipaddress.ip_network(IP))]
            changes.append(f"{slot}: appended {IP} to trusted_proxies")
        if conf.get("use_x_forwarded_for") is not True:
            conf["use_x_forwarded_for"] = True
            changes.append(f"{slot}: set use_x_forwarded_for")
    return changes


# ---------- before HA's YAML import: configuration.yaml ----------
def own_yaml_block():
    """The exact http: block this script appends to configuration.yaml."""
    return [f"# Trust the ngrok container as a reverse proxy ({TAG})", "http:",
            "  use_x_forwarded_for: true", "  trusted_proxies:", f"    - {IP}"]


def clean(item):
    """'  - "172.19.0.5"  # ngrok' -> '172.19.0.5'"""
    return item.split("#", 1)[0].strip().lstrip("-").strip().strip("'\"")


def patch_yaml(lines):
    """Return (new_lines, changes). Line-based so HA's !include/!secret tags survive."""
    head = next((i for i, l in enumerate(lines) if re.match(r"^http\s*:", l)), None)
    if head is None:
        return lines + [""] + own_yaml_block(), ["added http: section"]
    if not re.match(r"^http\s*:\s*(#.*)?$", lines[head]):
        raise ValueError(f"'{lines[head].strip()}' (e.g. !include) - edit that file by hand")

    # the http: block = following lines that are blank, comments, or indented
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    while end > head + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    body = range(head + 1, end)
    ind = next((re.match(r"^(\s+)", lines[i])[1] for i in body
                if lines[i].strip() and not lines[i].lstrip().startswith("#")), "  ")
    out, changes = list(lines), []

    # trusted_proxies first: it sits later in the block, so earlier indices stay valid
    tp = next((i for i in body if re.match(r"^\s+trusted_proxies\s*:", lines[i])), None)
    if tp is None:
        out[head + 1:head + 1] = [f"{ind}trusted_proxies:", f"{ind}  - {IP}  # {TAG}"]
        changes.append("added trusted_proxies")
    else:
        key_ind, rest = re.match(r"^(\s+)trusted_proxies\s*:\s*(.*)$", lines[tp]).groups()
        value = rest.split("#", 1)[0].strip()
        if value.startswith("["):  # inline list: [a, b]
            items = [clean(x) for x in value.strip("[]").split(",") if clean(x)]
            if not any(covers(x) for x in items):
                out[tp] = f"{key_ind}trusted_proxies: [{', '.join(items + [IP])}]  # {IP} {TAG}"
                changes.append(f"appended {IP} to trusted_proxies")
        elif value:  # single scalar
            if not covers(clean(value)):
                out[tp:tp + 1] = [f"{key_ind}trusted_proxies:", f"{key_ind}  - {clean(value)}",
                                  f"{key_ind}  - {IP}  # {TAG}"]
                changes.append(f"appended {IP} to trusted_proxies")
        else:  # block list of "- item" lines
            items, last, item_ind, j = [], tp, key_ind + "  ", tp + 1
            while j < end and (not lines[j].strip() or lines[j].lstrip().startswith(("-", "#"))
                               or len(lines[j]) - len(lines[j].lstrip()) > len(key_ind)):
                if lines[j].lstrip().startswith("-"):
                    items.append(clean(lines[j]))
                    last, item_ind = j, re.match(r"^(\s*)", lines[j])[1]
                j += 1
            if not any(covers(x) for x in items):
                out.insert(last + 1, f"{item_ind}- {IP}  # {TAG}")
                changes.append(f"appended {IP} to trusted_proxies")

    xf = next((i for i in body if re.match(r"^\s+use_x_forwarded_for\s*:", lines[i])), None)
    if xf is None:
        out.insert(head + 1, f"{ind}use_x_forwarded_for: true  # {TAG}")
        changes.append("added use_x_forwarded_for: true")
    elif not re.match(r"^\s+use_x_forwarded_for\s*:\s*(true|yes|on)\b", lines[xf], re.I):
        out[out.index(lines[xf])] = f"{ind}use_x_forwarded_for: true  # {TAG}"
        changes.append("set use_x_forwarded_for: true")
    return out, changes


def remove_own_yaml():
    """Drop the http: block this script added, once HA has imported it."""
    if not os.path.exists(CONF):
        return
    with open(CONF) as f:
        lines = f.read().splitlines()
    block = own_yaml_block()
    for i in range(len(lines) - len(block) + 1):
        if lines[i:i + len(block)] == block:
            start = i - 1 if i > 0 and not lines[i - 1].strip() else i
            backup(CONF)
            write_atomic(CONF, "\n".join(lines[:start] + lines[i + len(block):]) + "\n")
            log("removed the http: YAML block it added earlier (HA now stores this setting)")
            return


def main():
    if not IP:
        log("ERROR: NGROK_PROXY_IP not set - refusing to start Home Assistant")
        return 1
    try:
        ipaddress.ip_address(IP)
    except ValueError:
        log(f"ERROR: NGROK_PROXY_IP={IP!r} is not an IP address - refusing to start Home Assistant")
        return 1

    store = None
    if os.path.exists(STORE):
        with open(STORE) as f:
            store = json.load(f)
    if store is not None and store.get("data", {}).get("yaml_migration_done"):
        try:
            changes = patch_store(store)
        except (ValueError, KeyError, TypeError) as e:
            log(f"ERROR: cannot update .storage/http: {e} - refusing to start Home Assistant")
            return 1
        if changes:
            backup(STORE)
            write_atomic(STORE, json.dumps(store, indent=2) + "\n")
            log("; ".join(changes))
        else:
            log(f"{IP} already trusted (.storage/http) - no change")
        remove_own_yaml()
        return 0

    if not os.path.exists(CONF):
        # First start: have HA write its normal default config now, so it can be
        # patched before HA loads it (otherwise the first start would lack the trust).
        log("no configuration.yaml yet - creating Home Assistant's default config")
        subprocess.run([sys.executable, "-m", "homeassistant", "--script", "ensure_config",
                        "-c", CONFIG_DIR], check=True)
    with open(CONF) as f:
        lines = f.read().splitlines()
    try:
        new, changes = patch_yaml(lines)
    except ValueError as e:
        log(f"ERROR: cannot edit the http: section automatically: {e} - refusing to start Home Assistant")
        return 1
    if changes:
        backup(CONF)
        write_atomic(CONF, "\n".join(new) + "\n")
        log("; ".join(changes))
    else:
        log(f"{IP} already trusted (configuration.yaml) - no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
