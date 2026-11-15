#!/usr/bin/env python3
"""Make Home Assistant trust the ngrok container as a reverse proxy.

Runs inside the homeassistant container on every start, before HA itself
(see `entrypoint` in docker-compose.yaml). Without this, every request through
the tunnel gets "400: Bad Request".

It only ever adds: ngrok's IP is appended to trusted_proxies (and
use_x_forwarded_for is set to true, which HA requires alongside trusted_proxies).
Already trusted (the IP, or a subnet containing it) -> no change.

Where the setting lives depends on HA's state:
  * HA >= 2026.8 manages web-server settings in .storage/http. It imports the
    `http:` YAML once (yaml_migration_done) and ignores YAML afterwards. Once
    imported, this script edits the stored `stable` and `pending` configs, and
    removes the YAML block it added itself (HA flags leftover YAML as a repair).
  * Before that import (fresh install, or older HA), it appends to the `http:`
    section of configuration.yaml, which HA then imports on this start.

Any failure exits non-zero, which stops the container before HA starts
(docker-compose.yaml runs `this && exec /init`), so a misconfigured proxy
setup is loud instead of a silent "400: Bad Request" through the tunnel.

Env: NGROK_PROXY_IP (required), HA_CONFIG_DIR (default /config).
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
STORE_VERSION = 3  # format this script understands; anything else fails loudly
IP = os.environ.get("NGROK_PROXY_IP", "").strip()
TAG = "added by ha-trust-ngrok"


def own_yaml_block():
    """The exact http: block this script appends to configuration.yaml."""
    return [f"# Trust the ngrok container as a reverse proxy ({TAG})", "http:",
            "  use_x_forwarded_for: true", "  trusted_proxies:", f"    - {IP}"]


def log(msg):
    print(f"[ha-trust-ngrok] {msg}", flush=True)


def clean(item):
    """'  - "172.19.0.5"  # ngrok' -> '172.19.0.5'"""
    return item.split("#", 1)[0].strip().lstrip("-").strip().strip("'\"")


def covers(entry):
    try:
        return ipaddress.ip_address(IP) in ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return False


def patch(lines):
    """Return (new_lines, list_of_changes)."""
    head = next((i for i, l in enumerate(lines) if re.match(r"^http\s*:", l)), None)

    if head is None:
        return lines + [""] + own_yaml_block(), ["added http: section"]

    if not re.match(r"^http\s*:\s*(#.*)?$", lines[head]):
        raise ValueError(f"'{lines[head].strip()}' (e.g. !include) - edit that file by hand")

    # http: block = following lines that are blank, comments, or indented
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    while end > head + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    body = range(head + 1, end)
    ind = next((re.match(r"^(\s+)", lines[i])[1] for i in body
                if lines[i].strip() and not lines[i].lstrip().startswith("#")), "  ")
    out, changes = list(lines), []

    # --- trusted_proxies (edit first: it sits later in the block, so indices stay valid)
    tp = next((i for i in body if re.match(r"^\s+trusted_proxies\s*:", lines[i])), None)
    if tp is None:
        out.insert(head + 1, f"{ind}trusted_proxies:")
        out.insert(head + 2, f"{ind}  - {IP}  # {TAG}")
        changes.append("added trusted_proxies")
    else:
        key_ind, rest = re.match(r"^(\s+)trusted_proxies\s*:\s*(.*)$", lines[tp]).groups()
        rest_val = rest.split("#", 1)[0].strip()
        if rest_val.startswith("["):  # inline list: [a, b]
            items = [clean(x) for x in rest_val.strip("[]").split(",") if clean(x)]
            if not any(covers(x) for x in items):
                inner = ", ".join(items + [IP])
                out[tp] = f"{key_ind}trusted_proxies: [{inner}]  # {IP} {TAG}"
                changes.append(f"appended {IP} to trusted_proxies")
        elif rest_val:  # single scalar value
            if not covers(clean(rest_val)):
                out[tp] = f"{key_ind}trusted_proxies:"
                out.insert(tp + 1, f"{key_ind}  - {clean(rest_val)}")
                out.insert(tp + 2, f"{key_ind}  - {IP}  # {TAG}")
                changes.append(f"appended {IP} to trusted_proxies")
        else:  # block list of "- item" lines
            items, last, item_ind = [], tp, key_ind + "  "
            j = tp + 1
            while j < end and (not lines[j].strip() or lines[j].lstrip().startswith(("-", "#"))
                               or len(lines[j]) - len(lines[j].lstrip()) > len(key_ind)):
                if lines[j].lstrip().startswith("-"):
                    items.append(clean(lines[j]))
                    last, item_ind = j, re.match(r"^(\s*)", lines[j])[1]
                j += 1
            if not any(covers(x) for x in items):
                out.insert(last + 1, f"{item_ind}- {IP}  # {TAG}")
                changes.append(f"appended {IP} to trusted_proxies")

    # --- use_x_forwarded_for must be true when trusted_proxies is set
    xf = next((i for i in body if re.match(r"^\s+use_x_forwarded_for\s*:", lines[i])), None)
    if xf is None:
        out.insert(head + 1, f"{ind}use_x_forwarded_for: true  # {TAG}")
        changes.append("added use_x_forwarded_for: true")
    elif not re.match(r"^\s+use_x_forwarded_for\s*:\s*(true|yes|on)\b", lines[xf], re.I):
        out[out.index(lines[xf])] = f"{ind}use_x_forwarded_for: true  # {TAG}"
        changes.append("set use_x_forwarded_for: true")
    return out, changes


# ---------- HA >= 2026.8: settings stored in .storage/http ----------
def load_store():
    try:
        with open(STORE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def patch_store(store):
    """Ensure ngrok is trusted in the stored stable/pending configs. Returns changes."""
    if store.get("version") != STORE_VERSION:
        raise ValueError(f".storage/http is version {store.get('version')}, this script "
                         f"understands version {STORE_VERSION} - update ops/ha-trust-ngrok.py")
    changes = []
    for slot in ("stable", "pending"):
        conf = store["data"].get(slot)
        if conf is None:  # no pending trial config
            continue
        proxies = conf.get("trusted_proxies") or []
        if not any(covers(p) for p in proxies):
            conf["trusted_proxies"] = proxies + [str(ipaddress.ip_network(IP))]
            changes.append(f"{slot}: appended {IP} to trusted_proxies")
        if conf.get("use_x_forwarded_for") is not True:
            conf["use_x_forwarded_for"] = True
            changes.append(f"{slot}: set use_x_forwarded_for")
    return changes


def write_atomic(path, text):
    """Write via a temp file + rename, keeping the original's permissions."""
    tmp = f"{path}.ha-trust-ngrok.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def backup(path):
    shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}")


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

    store = load_store()
    if store is not None and store.get("data", {}).get("yaml_migration_done"):
        # HA already imported the YAML and now ignores it: edit the stored settings.
        try:
            changes = patch_store(store)
        except (ValueError, KeyError, TypeError) as e:
            log(f"ERROR: cannot update .storage/http: {e!r} - refusing to start Home Assistant")
            return 1
        if changes:
            backup(STORE)
            write_atomic(STORE, json.dumps(store, indent=2) + "\n")
            log("; ".join(changes))
        else:
            log(f"{IP} already trusted (.storage/http) - no change")
        remove_own_yaml()
        return 0

    # Before HA's one-time YAML import (fresh install): ensure the YAML; HA imports it.
    if not os.path.exists(CONF):
        # First boot: have HA write its normal default config now, so we can patch
        # it before HA loads it (otherwise the first start would lack proxy trust).
        log("no configuration.yaml yet - creating HA's default config")
        subprocess.run([sys.executable, "-m", "homeassistant", "--script", "ensure_config",
                        "-c", CONFIG_DIR], check=True)
    with open(CONF) as f:
        text = f.read()
    try:
        new, changes = patch(text.splitlines())
    except ValueError as e:
        log(f"ERROR: cannot edit http: section automatically: {e} - refusing to start Home Assistant")
        return 1
    if not changes:
        log(f"{IP} already trusted (configuration.yaml) - no change")
        return 0
    backup(CONF)
    write_atomic(CONF, "\n".join(new) + "\n")
    log("; ".join(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
