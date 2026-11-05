# QEMU OTA Tests

Automated counterpart of the update exercises in the UNO Q guide
(foundriesio/standalone-docs pull request #2), run on a QEMU CI image against
a local Foundries Update Server. `OTA_TEST_MACHINE` selects the machine for
both the build and the tests: `qemuarm64-secureboot` (default) or
`qemux86-64`.
A QEMU device registers with `fio-device-register`, then goes through:

1. `shellhttpd` on OS version 1, with no reboot.
2. OS version 2 with `shellhttpd`: the device reboots, aktualizr-lite
   finalizes the update, and the version 1 deployment stays for rollback.
3. OS version 1 with an App that cannot start: the install fails before the
   reboot, aktualizr-lite rolls back to version 2 without rebooting,
   `shellhttpd` runs again, and the failed Target is not retried. The client
   log of that boot must show no TLS error.
4. An OS that never completes a boot: the boot loader runs out of tries and
   boots version 2 again, and aktualizr-lite reports the update failed and
   does not retry it. qemux86-64 only, see below.
5. A second App on the running OS, then a new version of it: both are
   installed without a reboot or an OS change, and `shellhttpd` keeps
   running.
6. A device config naming only `shellhttpd`: the device stops the second
   App.
7. A reboot with no update pending: the same version and Apps come back,
   and nothing is downloaded again.

The update and reboot checks also fail when a Target is downloaded more
than once, which is how an install redone after the reboot shows up, for
example when the client looks for its deployments under the wrong ostree OS
name.

The server runs with test-mode authentication, so registration and `fiocli`
take a token in place of the browser approval the guide uses.

## Requirements

- `kas-container`, and room for two image builds, three on qemux86-64.
- `qemu-img`, and `qemu-system-aarch64` or `qemu-system-x86_64` (Ubuntu:
  `qemu-utils`, `qemu-system-arm` or `qemu-system-x86`). qemux86-64 uses KVM
  when `/dev/kvm` is accessible.
- `sshpass`, and Python 3 with `pip install -r requirements.txt`.
- `fioserver`, `fiocli`, and `composectl`. `make -C contrib/e2e build` in an
  update-server checkout provisions them; the checkout needs Git LFS for the
  web UI assets the binaries embed. Set `OTA_TEST_TOOLS` to its
  `contrib/e2e/.cache` when the checkout is not a sibling of meta-foundries.
- Docker and `openssl`: the failing App and the second App, both running
  `busybox` from Docker Hub, are published to a temporary local registry
  that serves TLS on the Docker bridge address, as composectl uses plain HTTP
  for any registry on a loopback address yet pulls only over HTTPS. The Apps
  are cached under `.cache` after the first run.
- Host ports 8080, 8443, and 2222 free, and port 5000 on the Docker bridge
  address.

## Build

From the repository root, with `KAS_WORK_DIR`, `DL_DIR`, and `SSTATE_DIR`
set as in `AGENTS.md`:

```sh
tests/qemu-ota/build.sh
```

It builds OS versions 1 and 2 from `ci/<machine>.yml` with the overlays in
`kas/`, and stages each version's `ostree_repo`, disk, and firmware under
`tests/qemu-ota/.cache/<machine>`. `OTA_TEST_KAS_EXTRA` appends more kas
overlays, colon-separated, such as `ci/include/ci.yml` on a CI runner.

## Run

```sh
cd tests/qemu-ota
pytest -s -v
```

The tests share one device and run in order. QEMU emulates arm64 without
KVM, so an arm64 run takes tens of minutes; `OTA_TEST_TIMEOUT` (seconds,
default 1800) bounds each wait. On failure the server log, the console tail,
and the device's aktualizr-lite journal are printed.

## Boot Counting

`kas/qemux86-64.yml` boots the qemux86-64 images through systemd-boot with
OSTree's boot counting, as the UNO Q does, where the CI image uses grub. Its
kernel arguments make a failed boot reboot. On machines with such an overlay,
`build.sh` also builds a broken OS, `kas/os-broken.yml`, whose `/sbin/init`
exits at once, through a class in the test-only `layer/`.

When an update keeps the number of deployments and the new one matches the
one it replaces in kernel, kernel arguments and commit version, OSTree keeps
the existing boot entries, so the new deployment boots without a counted
entry and a failed boot never falls back. `kas/ota-test.yml` therefore gives
every build its own `OSTREE_COMMIT_VERSION`.

The qemuarm64-secureboot image has no such overlay, and its fourth test is
skipped.

## CI

The qemu builds of `.github/workflows/kas-build.yml` run the tests right after
building their machine, reusing its build tree, on pushes to main and on pull
requests that change recipes, classes, `conf/`, `ci/`, these tests, or the
workflows that run them. `.github/workflows/qemu-ota-tests.yml` runs them on
both machines nightly and on demand. Both go through the
`.github/actions/qemu-ota-tests` action, which builds the server and CLI from
a pinned update-server commit and installs the host tools a runner lacks
through passwordless sudo.

## Not Covered Yet

- Rollback after a failed boot on qemuarm64-secureboot.
- A downgrade chosen by the operator, interrupted downloads, and running out
  of storage.
