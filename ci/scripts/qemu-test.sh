#!/bin/bash -e
# SPDX-License-Identifier: MIT
#
# Boot the machine's image in QEMU and update it, against a local update-server.
# Run after `kas build`, with the same environment:
#
#   KAS_CONTAINER, KAS_WORK_DIR, KAS_YAMLS (default ci/<machine>.yml)
#
#   ci/scripts/qemu-test.sh qemux86-64
#
# v1 is the commit the built image boots, and v2 the build with
# ci/tests/qemu/v2.yml. The tests run inside kas-container, so runqemu, the
# update-server and the tests share one network namespace.
# Logs (serial console, update-server, aktualizr-lite) go to
# $KAS_WORK_DIR/qemu-test/logs.

MACHINE=${1:?usage: $0 <machine>}
IMAGE=${QEMU_TEST_IMAGE:-core-image-full-cmdline}
UPDATE_SERVER_VERSION=${UPDATE_SERVER_VERSION:-v0.9.5}
KAS_CONTAINER=${KAS_CONTAINER:-$(which kas-container)}
KAS_YAMLS=${KAS_YAMLS:-ci/${MACHINE}.yml}
: "${KAS_WORK_DIR:?set KAS_WORK_DIR to the kas work directory of the build}"

TOPDIR=$(realpath "$(dirname "$(readlink -f "$0")")/../..")
cd "$TOPDIR"

DEPLOY=build/tmp/deploy/images/$MACHINE  # relative to KAS_WORK_DIR, mounted at /work
TEST=qemu-test
mkdir -p "$KAS_WORK_DIR/$TEST/bin"
rm -rf "$KAS_WORK_DIR/$TEST/logs"

commit() {
    local refs=("$KAS_WORK_DIR/$DEPLOY"/ostree_repo/refs/heads/*)
    [ ${#refs[@]} -eq 1 ] || { echo "expected one branch in ostree_repo, found: ${refs[*]}" >&2; exit 1; }
    cat "${refs[0]}"
}

# The v2 build replaces the deployed image; v1 boots from this copy, named so
# runqemu recognizes it as a rootfs.
V1_COMMIT=$(commit)
cp --dereference "$KAS_WORK_DIR/$DEPLOY/$IMAGE-$MACHINE.rootfs.wic.qcow2" "$KAS_WORK_DIR/$TEST/v1.rootfs.wic.qcow2"

"$KAS_CONTAINER" build "$KAS_YAMLS:ci/tests/qemu/v2.yml"
V2_COMMIT=$(commit)
[ "$V1_COMMIT" != "$V2_COMMIT" ] || { echo "v2 build produced the same commit as v1" >&2; exit 1; }

# fiocli uploads the ostree_repo/ of a directory; hardlinks keep it cheap.
rm -rf "$KAS_WORK_DIR/$TEST/upload"
mkdir -p "$KAS_WORK_DIR/$TEST/upload"
cp -al "$KAS_WORK_DIR/$DEPLOY/ostree_repo" "$KAS_WORK_DIR/$TEST/upload/ostree_repo"

for tool in fioserver fiocli; do
    if ! [ -x "$KAS_WORK_DIR/$TEST/bin/$tool-$UPDATE_SERVER_VERSION" ]; then
        curl -fsSL -o "$KAS_WORK_DIR/$TEST/bin/$tool-$UPDATE_SERVER_VERSION" \
            "https://github.com/foundriesio/update-server/releases/download/$UPDATE_SERVER_VERSION/$tool-linux-amd64"
        chmod +x "$KAS_WORK_DIR/$TEST/bin/$tool-$UPDATE_SERVER_VERSION"
    fi
    ln -sf "$tool-$UPDATE_SERVER_VERSION" "$KAS_WORK_DIR/$TEST/bin/$tool"
done

kas_args=()
kvm=0
if [ -w /dev/kvm ]; then
    kas_args=(--runtime-args "--device /dev/kvm --group-add $(stat -c %g /dev/kvm)")
    kvm=1
fi

"$KAS_CONTAINER" "${kas_args[@]}" shell --skip repos_checkout "$KAS_YAMLS" -c "
    cd /repo/ci/tests/qemu &&
    QEMU_TEST_MACHINE=$MACHINE QEMU_TEST_IMAGE=$IMAGE QEMU_TEST_KVM=$kvm \
    QEMU_TEST_DEPLOY_DIR=/work/$DEPLOY QEMU_TEST_V1_DISK=/work/$TEST/v1.rootfs.wic.qcow2 \
    QEMU_TEST_V1_COMMIT=$V1_COMMIT QEMU_TEST_V2_COMMIT=$V2_COMMIT \
    QEMU_TEST_UPLOAD_DIR=/work/$TEST/upload QEMU_TEST_TOOLS=/work/$TEST/bin QEMU_TEST_WORKDIR=/work/$TEST \
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
"
