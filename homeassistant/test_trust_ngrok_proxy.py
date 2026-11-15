"""Tests for trust-ngrok-proxy.py. Run from the repo root:

    python3 -m unittest discover homeassistant -v

Each test runs the script as a subprocess against a temporary config dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trust-ngrok-proxy.py")
IP = "172.19.0.5"


def store(stable_proxies, xff=True, pending=None, migrated=True, version=3):
    return {"version": version, "minor_version": 1, "key": "http",
            "data": {"stable": {"server_port": 8123, "use_x_forwarded_for": xff,
                                "trusted_proxies": stable_proxies},
                     "pending": pending, "yaml_migration_done": migrated}}


class TrustNgrokProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, ".storage"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, yaml=None, http_store=None):
        if yaml is not None:
            with open(os.path.join(self.dir, "configuration.yaml"), "w") as f:
                f.write(yaml)
        if http_store is not None:
            with open(os.path.join(self.dir, ".storage", "http"), "w") as f:
                json.dump(http_store, f)

    def run_script(self, ip=IP):
        env = {**os.environ, "HA_CONFIG_DIR": self.dir, "NGROK_PROXY_IP": ip}
        return subprocess.run([sys.executable, "-I", SCRIPT], env=env,
                              capture_output=True, text=True)

    def yaml(self):
        with open(os.path.join(self.dir, "configuration.yaml")) as f:
            return f.read()

    def stored(self):
        with open(os.path.join(self.dir, ".storage", "http")) as f:
            return json.load(f)["data"]

    # --- .storage/http (HA >= 2026.8, after the one-time YAML import) ---
    def test_store_already_trusted_by_subnet(self):
        self.write("default_config:\n", store(["172.19.0.0/16"]))
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("already trusted", r.stdout)

    def test_store_appends_to_stable_and_pending(self):
        self.write("default_config:\n",
                   store(["10.0.0.2/32"], xff=False, pending={"trusted_proxies": []}))
        self.assertEqual(self.run_script().returncode, 0)
        data = self.stored()
        self.assertEqual(data["stable"]["trusted_proxies"], ["10.0.0.2/32", "172.19.0.5/32"])
        self.assertTrue(data["stable"]["use_x_forwarded_for"])
        self.assertEqual(data["pending"]["trusted_proxies"], ["172.19.0.5/32"])
        self.assertIn("already trusted", self.run_script().stdout)  # idempotent

    def test_store_unknown_version_fails(self):
        self.write("default_config:\n", store([], version=4))
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR", r.stdout)

    def test_store_mode_leaves_user_yaml_alone(self):
        user_yaml = "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 172.19.0.5\n"
        self.write(user_yaml, store(["172.19.0.5/32"]))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.yaml(), user_yaml)

    # --- fresh install: YAML first, then HA imports it, then cleanup ---
    def test_fresh_install_flow(self):
        self.write("default_config:\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn(f"    - {IP}", self.yaml())
        self.write(http_store=store([f"{IP}/32"]))  # simulate HA importing the YAML
        r = self.run_script()
        self.assertIn("removed the http: YAML block", r.stdout)
        self.assertEqual(self.yaml(), "default_config:\n")

    # --- configuration.yaml variants (before the import) ---
    def test_yaml_appends_to_existing_block_list(self):
        self.write("http:\n  trusted_proxies:\n    - 10.0.0.2  # nginx\n  use_x_forwarded_for: false\nsun:\n")
        self.assertEqual(self.run_script().returncode, 0)
        y = self.yaml()
        self.assertIn("    - 10.0.0.2  # nginx\n    - 172.19.0.5", y)
        self.assertIn("use_x_forwarded_for: true", y)
        self.assertTrue(y.rstrip().endswith("sun:"))

    def test_yaml_inline_list(self):
        self.write("http:\n  use_x_forwarded_for: true\n  trusted_proxies: [10.0.0.2]\n")
        self.run_script()
        self.assertIn("trusted_proxies: [10.0.0.2, 172.19.0.5]", self.yaml())

    def test_yaml_include_fails(self):
        self.write("http: !include http.yaml\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR", r.stdout)

    # --- input validation ---
    def test_missing_or_bad_ip_fails(self):
        self.write("default_config:\n")
        self.assertEqual(self.run_script(ip="").returncode, 1)
        self.assertEqual(self.run_script(ip="not-an-ip").returncode, 1)


if __name__ == "__main__":
    unittest.main()
