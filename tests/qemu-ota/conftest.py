"""Fixtures: a local update-server and a QEMU device booted from OS version 1."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
MACHINE = os.environ.get("OTA_TEST_MACHINE", "qemuarm64-secureboot")
RELEASES = Path(os.environ.get("OTA_TEST_RELEASES", CACHE / MACHINE))
# update-server's `make -C contrib/e2e build` provisions these tools.
TOOLS = Path(os.environ.get(
    "OTA_TEST_TOOLS", HERE / "../../../update-server/contrib/e2e/.cache"))
TIMEOUT = int(os.environ.get("OTA_TEST_TIMEOUT", "1800"))

SERVER_NAME = "update-server.test"
# The QEMU user-mode network gateway, which is the host.
SERVER_ADDR = "10.0.2.2"
SSH_PORT = int(os.environ.get("OTA_TEST_SSH_PORT", "2222"))

TAG = "qemu-ota"
HARDWARE_ID = MACHINE
# composectl pulls the host architecture unless told otherwise.
APP_ARCH = "amd64" if MACHINE == "qemux86-64" else "arm64"
APP = "shellhttpd"
APP_URI = (
    "hub.foundries.io/lmp/shellhttpd"
    "@sha256:589b63dd3ab24a016472145101858fc5124970c10ef5cabfb8d877b90e198603"
)


def _tool(name: str) -> str:
    local = TOOLS / name
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found:
        pytest.exit(f"missing tool: {name}")
    return found


def wait_for(what: str, check, timeout: int = TIMEOUT, interval: int = 10):
    deadline = time.time() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.time() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(interval)


class QemuDevice:
    def __init__(self, release: Path, workdir: Path):
        self.workdir = workdir
        self.console = workdir / "console.log"
        self.disk = self._overlay(release / "disk.qcow2")
        self.cmd = self._platform(release) + [
            "-smp", "4", "-m", "2048",
            "-drive", f"file={self.disk},if=virtio,format=qcow2",
            "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-display", "none", "-serial", f"file:{self.console}",
        ]
        self.proc = None

    def _overlay(self, base: Path) -> Path:
        """Writes go to a per-run overlay, keeping the staged release clean."""
        path = self.workdir / base.name
        subprocess.run(
            [_tool("qemu-img"), "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(base), str(path)],
            check=True, capture_output=True)
        return path

    def _platform(self, release: Path) -> list[str]:
        if MACHINE == "qemux86-64":
            kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
            return [
                _tool("qemu-system-x86_64"),
                "-machine", "q35,i8042=off",
                *(["-enable-kvm", "-cpu", "host"] if kvm
                  else ["-cpu", "Skylake-Client"]),
                "-drive", "if=pflash,format=qcow2,readonly=on,"
                          f"file={release / 'ovmf.code.qcow2'}",
                "-drive", "if=pflash,format=qcow2,"
                          f"file={self._overlay(release / 'ovmf.vars.qcow2')}",
            ]
        return [
            _tool("qemu-system-aarch64"),
            "-machine", "virt,secure=on,acpi=off", "-cpu", "cortex-a57",
            "-bios", str(release / "flash.bin"),
        ]

    def start(self):
        self.proc = subprocess.Popen(self.cmd)
        wait_for("ssh", self.reachable, interval=5)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=30)

    def run(self, cmd: str, check: bool = True,
            timeout: int = 300) -> subprocess.CompletedProcess:
        res = subprocess.run(
            ["sshpass", "-p", "", "ssh", "-p", str(SSH_PORT),
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
             "root@127.0.0.1", cmd],
            capture_output=True, text=True, timeout=timeout)
        if check and res.returncode != 0:
            raise RuntimeError(
                f"{cmd!r} failed (rc={res.returncode})\n"
                f"stdout={res.stdout}\nstderr={res.stderr}")
        return res

    def reachable(self) -> bool:
        try:
            return self.run("true", check=False, timeout=30).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def boot_id(self) -> str:
        return self.run("cat /proc/sys/kernel/random/boot_id").stdout.strip()


@pytest.fixture(scope="session")
def tmp_session():
    # Next to the releases, so uploads can hard-link them.
    CACHE.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="run-", dir=CACHE))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def releases() -> Path:
    for v in (1, 2):
        if not (RELEASES / str(v) / "disk.qcow2").exists():
            pytest.exit(f"release {v} missing in {RELEASES}; run build.sh")
    return RELEASES


@pytest.fixture(scope="session")
def update_server(request, tmp_session):
    fioserver = _tool("fioserver")
    datadir = tmp_session / "datadir"
    for args in (["auth-init", "--test"],
                 ["pki-init", "--dnsname", SERVER_NAME],
                 ["tuf-init"]):
        subprocess.run([fioserver, "--datadir", str(datadir), *args],
                       check=True, capture_output=True)

    log_path = tmp_session / "server.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [fioserver, "serve", "--datadir", str(datadir)],
            stdout=log, stderr=log)

        def up():
            try:
                return requests.get("http://localhost:8080", timeout=2).ok
            except requests.exceptions.ConnectionError:
                return False

        wait_for("update-server", up, timeout=30, interval=1)
        yield datadir

        proc.terminate()
        proc.wait(timeout=10)
    if request.session.testsfailed:
        print("\n[update-server log]\n" + log_path.read_text())


@pytest.fixture(scope="session")
def fiocli(update_server):
    home = update_server / "fiocli-home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(home)}

    def run(*args) -> str:
        res = subprocess.run([_tool("fiocli"), *args],
                             capture_output=True, text=True, env=env)
        if res.returncode != 0:
            raise RuntimeError(f"fiocli {' '.join(args)}: {res.stderr}")
        return res.stdout

    run("login", "--token", "test", "qemu-ota", "http://localhost:8080")
    return run


@pytest.fixture(scope="session")
def app_content() -> Path:
    apps = CACHE / f"apps-{APP_ARCH}"
    if not (apps.exists() and any(apps.iterdir())):
        apps.mkdir(parents=True, exist_ok=True)
        subprocess.run([_tool("composectl"), "pull", "--arch", APP_ARCH,
                        f"-i{apps}", f"-s{apps}", APP_URI], check=True)
    return apps


@pytest.fixture(scope="session")
def device(request, releases, update_server, tmp_session):
    dev = QemuDevice(releases / "1", tmp_session)
    dev.start()
    yield dev
    dev.stop()
    if request.session.testsfailed:
        print("\n[console tail]\n"
              + "\n".join(dev.console.read_text().splitlines()[-200:]))


@pytest.fixture(scope="session")
def registered_device(device) -> str:
    device.run(f"echo '{SERVER_ADDR} {SERVER_NAME}' >> /etc/hosts")
    device.run("mkdir -p /etc/sota/conf.d && printf "
               "'[uptane]\\npolling_sec = 15\\n' "
               "> /etc/sota/conf.d/z-90-polling.toml")
    device.run("systemctl stop aktualizr-lite")
    # Test-mode auth accepts any token, standing in for the browser approval.
    device.run(
        f"fio-device-register --api-token test "
        f"--device-api http://{SERVER_NAME}:8080/v1/devices "
        f"--name qemu-01 --tag {TAG}",
        timeout=600)
    device.run("systemctl start aktualizr-lite fioconfig")

    # A rollout only reaches devices whose tag the server knows, and the tag
    # arrives with aktualizr-lite's first TUF request, after other check-ins.
    def checked_in():
        devices = requests.get("http://localhost:8080/v1/devices",
                               timeout=5).json()
        return next((d["uuid"] for d in devices
                     if d.get("last-seen", 0) > 0 and d.get("tag") == TAG),
                    None)

    return wait_for("device check-in", checked_in, timeout=600)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On a failed test, show what the client did; the journal is volatile and
    gone after the next reboot."""
    outcome = yield
    report = outcome.get_result()
    dev = item.funcargs.get("device")
    if report.when != "call" or not report.failed or dev is None:
        return
    try:
        journal = dev.run("journalctl -u aktualizr-lite --no-pager | tail -n 200",
                          check=False, timeout=120).stdout
    except subprocess.TimeoutExpired:
        journal = "(device unreachable)"
    report.sections.append(("aktualizr-lite journal", journal))
