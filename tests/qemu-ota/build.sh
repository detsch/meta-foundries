#!/bin/sh -e
# Builds OS versions 1 and 2 and stages what the tests boot and upload.
#
# Usage: [OTA_TEST_MACHINE=<machine>] tests/qemu-ota/build.sh [OUT_DIR]

TOPDIR=$(realpath "$(dirname "$(readlink -f "$0")")/../..")
MACHINE=${OTA_TEST_MACHINE:-qemuarm64-secureboot}
OUT=$(realpath -m "${1:-${OTA_TEST_OUT:-$TOPDIR/tests/qemu-ota/.cache/$MACHINE}}")
IMAGE=core-image-full-cmdline
KAS_CONTAINER=${KAS_CONTAINER:-$(which kas-container)}
: "${KAS_WORK_DIR:?set KAS_WORK_DIR outside the repository}"

DEPLOY="$KAS_WORK_DIR/build/tmp/deploy/images/$MACHINE"

cd "$TOPDIR"
for v in 1 2; do
    "$KAS_CONTAINER" build \
        "ci/$MACHINE.yml:tests/qemu-ota/kas/ota-test.yml:tests/qemu-ota/kas/os-v$v.yml"

    # The deploy ostree_repo is shared across builds, so stage each one.
    rm -rf "${OUT:?}/$v"
    mkdir -p "$OUT/$v"
    cp -a "$DEPLOY/ostree_repo" "$OUT/$v/ostree_repo"
    cp -L "$DEPLOY/$IMAGE-$MACHINE.rootfs.wic.qcow2" "$OUT/$v/disk.qcow2"
    case "$MACHINE" in
    qemux86-64)
        cp -L "$DEPLOY/ovmf.code.qcow2" "$DEPLOY/ovmf.vars.qcow2" "$OUT/$v/" ;;
    *)
        cp -L "$DEPLOY/flash.bin" "$OUT/$v/flash.bin" ;;
    esac
done

echo "Staged releases in $OUT"
