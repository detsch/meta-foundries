# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
# An OS that panics right after the initramfs switches root, so its
# deployment never completes a boot.
ROOTFS_POSTPROCESS_COMMAND:append:pn-core-image-full-cmdline = " qemu_ota_break_init"

qemu_ota_break_init() {
    ln -sf ${base_bindir}/false ${IMAGE_ROOTFS}${base_sbindir}/init
}
