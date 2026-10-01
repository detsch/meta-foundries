# SPDX-License-Identifier: MIT
"""Helpers for the QEMU tests: a local update-server, fiocli, and a runqemu guest.

Runs inside kas-container (see ci/scripts/qemu-test.sh). The guest reaches the
server at 10.0.2.2 (slirp), and the tests reach its sshd at 127.0.0.1:2222.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

# Per-machine runqemu options; the rest comes from the image's qemuboot.conf.
MACHINES = {
    "qemux86-64": {"runqemu": ["ovmf"], "kvm": True},
    # TF-A and OP-TEE boot from flash.bin; secure=on rules out KVM.
    "qemuarm64-secureboot": {"runqemu": [], "kvm": False},
}

SERVER_NAME = "update-server"  # the gateway certificate's DNS name
API = "http://127.0.0.1:8080"
TOKEN = "e2e-local-dev"  # any token works with `auth-init --test`
SSH_PORT = 2222


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set; run this through ci/scripts/qemu-test.sh")
    return value


class Config:
    def __init__(self):
        self.machine = env("QEMU_TEST_MACHINE")
        if self.machine not in MACHINES:
            raise RuntimeError(f"no QEMU test support for {self.machine}")
        self.image = env("QEMU_TEST_IMAGE")
        self.deploy_dir = env("QEMU_TEST_DEPLOY_DIR")
        self.v1_disk = env("QEMU_TEST_V1_DISK")
        self.v1_commit = env("QEMU_TEST_V1_COMMIT")
        self.v2_commit = env("QEMU_TEST_V2_COMMIT")
        self.upload_dir = env("QEMU_TEST_UPLOAD_DIR")  # holds ostree_repo/, as fiocli expects
        self.tools = env("QEMU_TEST_TOOLS")
        self.workdir = env("QEMU_TEST_WORKDIR")
        self.tag = env("QEMU_TEST_TAG", "master")
        self.kvm = env("QEMU_TEST_KVM", "0") == "1" and MACHINES[self.machine]["kvm"]
        # A fully emulated guest is several times slower.
        self.timeout_scale = 1 if self.kvm else 6
        self.logs = os.path.join(self.workdir, "logs")
        os.makedirs(self.logs, exist_ok=True)


def wait_for(what, check, timeout, interval=3):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


class Server:
    """fioserver in test mode, with fiocli logged in to it."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.datadir = os.path.join(cfg.workdir, "server-data")
        self.home = os.path.join(cfg.workdir, "fiocli-home")
        self.proc = None

    def _fioserver(self, *args):
        subprocess.run([os.path.join(self.cfg.tools, "fioserver"), "--datadir", self.datadir, *args],
                       check=True, capture_output=True, text=True)

    def start(self):
        shutil.rmtree(self.datadir, ignore_errors=True)
        os.makedirs(self.datadir)
        self._fioserver("auth-init", "--test")
        self._fioserver("pki-init", "--dnsname", SERVER_NAME)
        self._fioserver("tuf-init")
        log = open(os.path.join(self.cfg.logs, "update-server.log"), "w")
        self.proc = subprocess.Popen([os.path.join(self.cfg.tools, "fioserver"), "--datadir", self.datadir, "serve"],
                                     stdout=log, stderr=subprocess.STDOUT)
        wait_for("update-server", self._up, 60, interval=1)
        os.makedirs(os.path.join(self.home, ".config"), exist_ok=True)
        self.fiocli("login", "ci", API, "--token", TOKEN)

    def _up(self):
        try:
            urllib.request.urlopen(API, timeout=2)
        except urllib.error.HTTPError:
            pass  # any HTTP status means it is serving
        except OSError:
            return False
        return True

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=30)

    def fiocli(self, *args):
        res = subprocess.run([os.path.join(self.cfg.tools, "fiocli"), *args], capture_output=True, text=True,
                             env={**os.environ, "HOME": self.home})
        if res.returncode != 0:
            raise RuntimeError(f"fiocli {' '.join(args)} failed:\n{res.stdout}{res.stderr}")
        return res.stdout

    def get(self, path):
        req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.load(res)

    def publish(self, version, commit, upload_dir):
        name = f"v{version}"
        self.fiocli("updates", "upload", self.cfg.tag, name, upload_dir, "--version", str(version),
                    "--hardware-id", self.cfg.machine, "--name", f"{self.cfg.machine}-lmp-{version}",
                    "--ostree-hash", commit)
        return name

    def checked_in(self, uuid):
        # The device reports its tag on its first gateway request.
        try:
            return self.get(f"/v1/devices/{uuid}").get("tag") == self.cfg.tag
        except urllib.error.HTTPError:
            return False

    def roll_out(self, update, uuid):
        rollout = f"r-{update}-{int(time.time())}"
        self.fiocli("updates", "create-rollout", update, rollout, "--uuids", uuid)
        # The server applies a rollout asynchronously.
        wait_for(f"rollout {rollout}",
                 lambda: uuid in self.get(f"/v1/updates/{update}/rollouts/{rollout}").get("effective-uuids", []),
                 60, interval=1)


class Guest:
    """The image booted with runqemu, reachable over ssh as root."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None

    def boot(self):
        qemuboot = os.path.join(self.cfg.deploy_dir, f"{self.cfg.image}-{self.cfg.machine}.rootfs.qemuboot.conf")
        args = ["runqemu", qemuboot, self.cfg.v1_disk, "slirp", "nographic", "snapshot",
                *MACHINES[self.cfg.machine]["runqemu"]]
        if self.cfg.kvm:
            args.append("kvm")
        self.serial = open(os.path.join(self.cfg.logs, "serial.log"), "w")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.serial, stderr=subprocess.STDOUT)
        self.wait_ssh()

    def wait_ssh(self):
        def up():
            if self.proc.poll() is not None:
                raise RuntimeError(f"runqemu exited with {self.proc.returncode}; see serial.log")
            return self.ssh("true", check=False).returncode == 0
        wait_for("guest ssh", up, 300 * self.cfg.timeout_scale, interval=5)

    def ssh(self, command, check=True, timeout=120):
        res = subprocess.run(
            ["ssh", "-q", "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "root@127.0.0.1", command],
            capture_output=True, text=True, timeout=timeout * self.cfg.timeout_scale)
        if check and res.returncode != 0:
            raise RuntimeError(f"guest command failed ({res.returncode}): {command}\n{res.stdout}{res.stderr}")
        return res

    def journal(self, unit, boot="0"):
        return self.ssh(f"journalctl -b {boot} -u {unit} --no-pager").stdout

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.ssh("poweroff", check=False, timeout=10)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
