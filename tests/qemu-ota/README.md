# QEMU OTA Tests

Automated counterpart of the update exercises in the UNO Q guide
(meta-foundries pull request #52), run on a QEMU CI image against a local
Foundries Update Server. `OTA_TEST_MACHINE` selects the machine for both the
build and the tests: `qemuarm64-secureboot` (default) or `qemux86-64`.
A QEMU device registers with `fio-device-register`, then aktualizr-lite
installs:

1. `shellhttpd` on OS version 1, with no reboot.
2. OS version 2 with `shellhttpd`: the device reboots, aktualizr-lite
   finalizes the update, and the version 1 deployment stays for rollback.

Both checks also fail when a Target is downloaded more than once, which is
how an install redone after the reboot shows up, for example when the client
looks for its deployments under the wrong ostree OS name.

The server runs with test-mode authentication, so registration and `fiocli`
take a token in place of the browser approval the guide uses.

## Requirements

- `kas-container`, and room for two image builds.
- `qemu-img`, and `qemu-system-aarch64` or `qemu-system-x86_64` (Ubuntu:
  `qemu-utils`, `qemu-system-arm` or `qemu-system-x86`). qemux86-64 uses KVM
  when `/dev/kvm` is accessible.
- `sshpass`, and Python 3 with `pip install -r requirements.txt`.
- `fioserver`, `fiocli`, and `composectl`. `make -C contrib/e2e build` in an
  update-server checkout provisions them; the checkout needs Git LFS for the
  web UI assets the binaries embed. Set `OTA_TEST_TOOLS` to its
  `contrib/e2e/.cache` when the checkout is not a sibling of meta-foundries.
- Host ports 8080, 8443, and 2222 free.

## Build

From the repository root, with `KAS_WORK_DIR`, `DL_DIR`, and `SSTATE_DIR`
set as in `AGENTS.md`:

```sh
tests/qemu-ota/build.sh
```

It builds OS versions 1 and 2 from `ci/<machine>.yml` with the overlays in
`kas/`, and stages each version's `ostree_repo`, disk, and firmware under
`tests/qemu-ota/.cache/<machine>`.

## Run

```sh
cd tests/qemu-ota
pytest -s -v
```

The tests share one device and run in order. QEMU emulates arm64 without
KVM, so an arm64 run takes tens of minutes; `OTA_TEST_TIMEOUT` (seconds,
default 1800) bounds each wait. On failure the server log, the console tail,
and the device's aktualizr-lite journal are printed.

## Not Covered Yet

- Rollback when an app fails to start after an OS update: needs a broken app
  published to a registry the test controls.
- Rollback after a failed boot: the image boots through grub-efi, which
  meta-updater configures without boot counting.
- The guide's application-only exercises (adding an app, updating one).
