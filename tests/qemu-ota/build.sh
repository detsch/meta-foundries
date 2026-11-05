#!/bin/sh -e
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
# Builds OS versions 1 and 2, and a broken OS on machines whose
# kas/<machine>.yml enables boot counting, and stages what the tests boot
# and upload.
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
KAS_YAMLS="ci/$MACHINE.yml:tests/qemu-ota/kas/ota-test.yml"
VERSIONS="1 2"
if [ -f "tests/qemu-ota/kas/$MACHINE.yml" ]; then
    KAS_YAMLS="$KAS_YAMLS:tests/qemu-ota/kas/$MACHINE.yml"
    VERSIONS="$VERSIONS broken"
fi
for v in $VERSIONS; do
    os="os-v$v"
    [ "$v" = broken ] && os="os-broken"
    "$KAS_CONTAINER" build \
        "$KAS_YAMLS:tests/qemu-ota/kas/$os.yml"

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
