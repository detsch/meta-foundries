# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: MIT

"""OS and app updates on a QEMU device, following the UNO Q guide's exercises.

Tests run in order on one device: each starts from the state the previous
one left.
"""

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from conftest import APP, HARDWARE_ID, TAG, wait_for


def _stage(tmp: Path, version: int, ostree_repo: Path, apps: Path) -> Path:
    """Lay out an upload directory; hard links keep the copy cheap."""
    out = tmp / f"upload-{version}"
    out.mkdir()
    subprocess.run(["cp", "-al", str(ostree_repo), str(out / "ostree_repo")],
                   check=True)
    subprocess.run(["cp", "-al", str(apps), str(out / "apps")], check=True)
    return out


def _deliver(fiocli, uuid: str, content: Path, version: int):
    name = f"qemu-ota-{version:03}"
    fiocli("updates", "upload", TAG, name, str(content),
           "--version", str(version), "--hardware-id", HARDWARE_ID,
           "--name", f"{HARDWARE_ID}-{version}")
    fiocli("updates", "create-rollout", name, f"rollout-{version}",
           "--uuids", uuid)


def _applied_version(device) -> int | None:
    res = device.run("aktualizr-lite status --json 1", check=False)
    if res.returncode != 0:
        return None
    return json.loads(res.stdout).get("applied_target", {}).get("version")


def _installed(device, version: int):
    def done():
        if not device.reachable():
            return False
        return _applied_version(device) == version
    return done


def _app_running(device) -> bool:
    return APP in device.run("docker ps --format '{{.Names}}'").stdout


def _assert_server_success(fiocli, uuid: str):
    corr_id = fiocli("devices", "updates", uuid).splitlines()[1].strip()
    events = fiocli("devices", "updates", uuid, corr_id)
    assert "EcuInstallationCompleted" in events and "-> Succeeded" in events, \
        events


def _assert_single_download(fiocli, uuid: str):
    """A Target downloaded twice means its install was redone, e.g. after a
    reboot that lost the pending record."""
    ids = [line.strip() for line in
           fiocli("devices", "updates", uuid).splitlines()[1:] if line.strip()]
    starts = Counter()
    log = []
    for corr_id in ids:
        events = fiocli("devices", "updates", uuid, corr_id)
        log.append(events)
        starts.update(re.findall(r"EcuDownloadStarted\(([^)]*)\)", events))
    assert starts and all(n == 1 for n in starts.values()), \
        f"downloads per Target: {dict(starts)}\n" + "\n".join(log)


def test_app_on_os_v1(fiocli, registered_device, device, releases,
                      app_content, tmp_session):
    content = _stage(tmp_session, 1, releases / "1" / "ostree_repo",
                     app_content)
    boot_id = device.boot_id()
    _deliver(fiocli, registered_device, content, 1)

    wait_for("version 1", _installed(device, 1))
    assert device.boot_id() == boot_id, "app-only update rebooted the device"
    wait_for(f"{APP} running", lambda: _app_running(device), timeout=300)
    _assert_server_success(fiocli, registered_device)
    _assert_single_download(fiocli, registered_device)


def test_os_update_v2(fiocli, registered_device, device, releases,
                      app_content, tmp_session):
    content = _stage(tmp_session, 2, releases / "2" / "ostree_repo",
                     app_content)
    boot_id = device.boot_id()
    _deliver(fiocli, registered_device, content, 2)

    # aktualizr-lite reboots the device and finalizes the update after boot.
    wait_for("version 2", _installed(device, 2))
    assert device.boot_id() != boot_id, "OS update did not reboot"
    assert 'BUILD_ID="qemu-ota-os-2"' in device.run(
        "cat /usr/lib/os-release").stdout
    status = device.run("ostree admin status").stdout
    deployments = re.findall(r"^[* ] nodistro \w+\.\d+", status, re.M)
    assert len(deployments) == 2, \
        f"previous deployment not kept for rollback:\n{status}"
    wait_for(f"{APP} running", lambda: _app_running(device), timeout=300)
    _assert_server_success(fiocli, registered_device)
    _assert_single_download(fiocli, registered_device)
    journal = device.run("journalctl -b -u aktualizr-lite --no-pager").stdout
    assert "Finalizing pending installation" in journal, journal


def _install_failed(fiocli, uuid: str, version: int):
    def done():
        for corr_id in fiocli("devices", "updates", uuid).splitlines()[1:]:
            events = fiocli("devices", "updates", uuid, corr_id.strip())
            if re.search(rf"EcuInstallationCompleted\([^)]*-{version}\) "
                         r"-> Failed", events):
                return True
        return False
    return done


def _only_booted_pending(device) -> bool:
    status = device.run("ostree admin status").stdout
    deployments = re.findall(r"^([* ]) nodistro (\w+)\.\d+(.*)$", status, re.M)
    booted = next(csum for mark, csum, _ in deployments if mark == "*")
    return all(csum == booted for _, csum, note in deployments
               if "pending" in note)


def test_rollback_on_app_failure(fiocli, registered_device, device, releases,
                                 broken_app_content, tmp_session):
    """OS version 1 with an App that cannot start: the install fails before
    the reboot, and aktualizr-lite rolls back to version 2."""
    content = _stage(tmp_session, 3, releases / "1" / "ostree_repo",
                     broken_app_content)
    boot_id = device.boot_id()
    _deliver(fiocli, registered_device, content, 3)

    wait_for("version 3 to fail", _install_failed(fiocli, registered_device, 3))
    wait_for("rollback to version 2",
             lambda: "Rolling back to the previous target" in device.run(
                 "journalctl -b -u aktualizr-lite --no-pager").stdout,
             timeout=600)
    # The rollback undeploys the failed OS by deploying the booted commit
    # again, so a pending deployment may remain, with the booted commit.
    wait_for("failed OS to be undeployed", lambda: _only_booted_pending(device),
             timeout=600)
    assert _applied_version(device) == 2
    assert device.boot_id() == boot_id, "rollback rebooted the device"
    assert 'BUILD_ID="qemu-ota-os-2"' in device.run(
        "cat /usr/lib/os-release").stdout
    wait_for(f"{APP} running", lambda: _app_running(device), timeout=300)

    # A failed Target is skipped from then on: let the client poll a few
    # times, then check it was downloaded once.
    time.sleep(60)
    _assert_single_download(fiocli, registered_device)
