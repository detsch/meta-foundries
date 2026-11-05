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

import pytest

from conftest import APP, HARDWARE_ID, SECOND_APP, TAG, wait_for


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


def _running(device, app: str) -> bool:
    return app in device.run("docker ps --format '{{.Names}}'").stdout


def _app_running(device) -> bool:
    return _running(device, APP)


def _booted_commit(device) -> str:
    return re.search(r"^\* nodistro (\w+)\.",
                     device.run("ostree admin status").stdout, re.M).group(1)


def _second_app_version(device) -> str:
    env = device.run(
        "docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "
        f"$(docker ps -q --filter name={SECOND_APP})").stdout
    return re.search(r"^APP_VERSION=(\S+)$", env, re.M).group(1)


def _assert_no_tls_errors(device):
    """The client verifies every remote it pulls from, the gateway's own
    download URL included."""
    journal = device.run("journalctl -b -u aktualizr-lite --no-pager").stdout
    for error in ("SSL peer certificate", "unknown authority"):
        assert error not in journal, f"TLS error in the client log:\n{journal}"


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
    _assert_no_tls_errors(device)

    # A failed Target is skipped from then on: let the client poll a few
    # times, then check it was downloaded once.
    time.sleep(60)
    _assert_single_download(fiocli, registered_device)


def test_rollback_on_boot_failure(fiocli, registered_device, device, releases,
                                  app_content, tmp_session):
    """An OS that never completes a boot: the boot loader runs out of tries
    and boots the previous deployment, and aktualizr-lite rolls back to
    version 2."""
    broken = releases / "broken" / "ostree_repo"
    if not broken.exists():
        pytest.skip("this machine boots without boot counting")
    content = _stage(tmp_session, 4, broken, app_content)
    boot_id = device.boot_id()
    _deliver(fiocli, registered_device, content, 4)

    wait_for("version 4 to fail", _install_failed(fiocli, registered_device, 4))
    assert device.boot_id() != boot_id, "the broken OS was never booted"
    assert "Attempted to kill init" in device.console.read_text(
        errors="replace"), "the broken OS did not fail its boot"
    assert _applied_version(device) == 2
    assert 'BUILD_ID="qemu-ota-os-2"' in device.run(
        "cat /usr/lib/os-release").stdout
    wait_for(f"{APP} running", lambda: _app_running(device), timeout=300)

    # A failed Target is skipped from then on: let the client poll a few
    # times, then check it was downloaded once.
    time.sleep(60)
    _assert_single_download(fiocli, registered_device)


def test_add_app_without_os_update(fiocli, registered_device, device,
                                   releases, second_app_content, tmp_session):
    """A second App on the running OS: installed without a reboot."""
    content = _stage(tmp_session, 5, releases / "2" / "ostree_repo",
                     second_app_content(1))
    boot_id, commit = device.boot_id(), _booted_commit(device)
    _deliver(fiocli, registered_device, content, 5)

    wait_for("version 5", _installed(device, 5))
    assert device.boot_id() == boot_id, "App update rebooted the device"
    assert _booted_commit(device) == commit
    wait_for(f"{SECOND_APP} running", lambda: _running(device, SECOND_APP),
             timeout=300)
    assert _second_app_version(device) == "1"
    assert _app_running(device)
    _assert_server_success(fiocli, registered_device)
    _assert_single_download(fiocli, registered_device)


def test_update_app_without_os_update(fiocli, registered_device, device,
                                      releases, second_app_content,
                                      tmp_session):
    """A new version of the second App: replaced without a reboot, and the
    other App keeps running."""
    content = _stage(tmp_session, 6, releases / "2" / "ostree_repo",
                     second_app_content(2))
    boot_id, commit = device.boot_id(), _booted_commit(device)
    _deliver(fiocli, registered_device, content, 6)

    wait_for("version 6", _installed(device, 6))
    assert device.boot_id() == boot_id, "App update rebooted the device"
    assert _booted_commit(device) == commit
    wait_for(f"{SECOND_APP} version 2",
             lambda: _running(device, SECOND_APP)
             and _second_app_version(device) == "2", timeout=300)
    assert _app_running(device)
    _assert_server_success(fiocli, registered_device)
    _assert_single_download(fiocli, registered_device)


def test_select_apps_by_config(fiocli, registered_device, device):
    """A device config naming one App: the device stops running the other."""
    fiocli("configs", "updates", "--device", registered_device, "--apps", APP)
    # fioconfig fetches its config when it starts.
    device.run("systemctl restart fioconfig")

    wait_for(f"{SECOND_APP} to stop",
             lambda: not _running(device, SECOND_APP), timeout=600)
    assert _app_running(device)
    assert fiocli("configs", "applied", registered_device).strip()


def test_reboot_keeps_state(fiocli, registered_device, device):
    """A reboot with no update pending: same version and Apps, and nothing
    downloaded again."""
    boot_id = device.boot_id()
    device.run("systemctl reboot", check=False, timeout=30)

    wait_for("reboot", lambda: device.reachable()
             and device.boot_id() != boot_id, timeout=600)
    assert _applied_version(device) == 6
    wait_for(f"{APP} running", lambda: _app_running(device), timeout=300)
    # Let the client poll a few times after the boot.
    time.sleep(60)
    assert not _running(device, SECOND_APP)
    assert _applied_version(device) == 6
    _assert_single_download(fiocli, registered_device)
