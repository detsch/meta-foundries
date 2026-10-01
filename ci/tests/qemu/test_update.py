# SPDX-License-Identifier: MIT
"""Register the image with a local update-server, then update it from v1 to v2.

v1 is the commit the image boots and v2 the build with ci/tests/qemu/v2.yml.
The steps run in order and share one guest; a failed step skips the rest.
"""

import os
import re
import subprocess
import unittest

from qemutest import Config, Guest, Server, SERVER_NAME, wait_for

cfg = server = guest = None
state = {}


def setUpModule():
    global cfg, server, guest
    cfg = Config()
    server = Server(cfg)
    server.start()
    state["v1"] = server.publish(1, cfg.v1_commit, cfg.upload_dir)
    state["v2"] = server.publish(2, cfg.v2_commit, cfg.upload_dir)
    guest = Guest(cfg)
    guest.boot()


def tearDownModule():
    if guest:
        try:
            with open(os.path.join(cfg.logs, "aktualizr-lite.log"), "w") as f:
                f.write(guest.ssh("journalctl -u aktualizr-lite --no-pager", check=False).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        guest.stop()
    if server:
        server.stop()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        if state.get("failed"):
            self.skipTest(f"an earlier step failed: {state['failed']}")

    def run(self, result=None):
        failures = len(result.failures) + len(result.errors) if result else 0
        super().run(result)
        if result and len(result.failures) + len(result.errors) > failures:
            state.setdefault("failed", self.id().split(".")[-1])

    def booted_and_rollback(self):
        # `ostree admin status` marks the booted deployment with "*".
        status = guest.ssh("ostree admin status").stdout
        booted = re.search(r"^\* \S+ ([0-9a-f]{64})\.", status, re.M)
        rollback = re.search(r"^  \S+ ([0-9a-f]{64})\.\d+ \(rollback\)", status, re.M)
        return (booted.group(1) if booted else None, rollback.group(1) if rollback else None)

    def restart_aklite(self):
        # A restart makes the daemon check for updates immediately.
        since = guest.ssh("date +%s").stdout.strip()
        guest.ssh("systemctl restart aktualizr-lite")
        return since

    def aklite_log_since(self, since):
        return guest.ssh(f"journalctl -u aktualizr-lite --no-pager --since @{since}").stdout

    def test_1_boots_v1(self):
        self.assertEqual(self.booted_and_rollback()[0], cfg.v1_commit)

    def test_2_register(self):
        guest.ssh(f"grep -q ' {SERVER_NAME}$' /etc/hosts || echo '10.0.2.2 {SERVER_NAME}' >> /etc/hosts")
        out = guest.ssh(f"DEVICE_API=http://{SERVER_NAME}:8080/v1/devices fio-device-register "
                        f"--api-token 'Bearer e2e-local-dev' --api-token-header Authorization --tag {cfg.tag}").stdout
        uuid = re.search(r"UUID: ([0-9a-f-]{36})", out)
        self.assertIsNotNone(uuid, out)
        state["uuid"] = uuid.group(1)
        self.assertIn(f'server = "https://{SERVER_NAME}:8443"', guest.ssh("cat /var/sota/sota.toml").stdout)
        # A rollout only assigns devices that have already checked in with their tag.
        wait_for("device check-in", lambda: server.checked_in(state["uuid"]), 120 * cfg.timeout_scale)

    def test_3_v1_is_current(self):
        server.roll_out(state["v1"], state["uuid"])
        since = self.restart_aklite()
        log = wait_for("aklite to apply v1",
                       lambda: (lambda l: l if "No reboot needed" in l else None)(self.aklite_log_since(since)),
                       180 * cfg.timeout_scale)
        self.assertIn(f"Target {cfg.v1_commit} is same as current", log)

    def test_4_update_to_v2(self):
        boot_id = guest.ssh("cat /proc/sys/kernel/random/boot_id").stdout.strip()
        server.roll_out(state["v2"], state["uuid"])
        self.restart_aklite()

        # aklite pulls and installs v2, then reboots the guest itself.
        def rebooted():
            try:
                res = guest.ssh("cat /proc/sys/kernel/random/boot_id", check=False, timeout=30)
            except subprocess.TimeoutExpired:
                return False
            return res.returncode == 0 and res.stdout.strip() != boot_id
        wait_for("the guest to reboot into v2", rebooted, 600 * cfg.timeout_scale, interval=5)

        wait_for("aklite to finalize v2",
                 lambda: "Finalization has been completed successfully" in guest.journal("aktualizr-lite"),
                 180 * cfg.timeout_scale)
        self.assertEqual(self.booted_and_rollback(), (cfg.v2_commit, cfg.v1_commit))
        self.assertIn('QEMU_TEST_TARGET="v2"', guest.ssh("cat /etc/os-release").stdout)
        device = server.get(f"/v1/devices/{state['uuid']}")
        self.assertEqual(device.get("ostree-hash"), cfg.v2_commit, device)


if __name__ == "__main__":
    unittest.main(verbosity=2)
