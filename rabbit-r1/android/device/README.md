# haretic Android device configuration

This is haretic's initial LineageOS 24 product for the rabbit r1 mainline kernel in this
workspace. It is not a tested Android image. The diagnostic kernel ZIP remains
the only assembled boot package.

The source lives in `rabbit-r1/android/device` in the kernel repository. Install
it into `device/rabbit/r1` with `scripts/install-lineage-device.py`; edit the
canonical copy rather than the installed files. The installer checks kernel
and module hashes, wraps the eMMC-enabled DTB in the table expected by LK, and
installs the charging feature. It allows updates to its own unchanged files and
refuses to overwrite local edits.

The product uses a 64-bit userspace, the stock v2 boot header and load addresses,
recovery in boot, and separate system, system_ext, product, and vendor logical
partitions. `/metadata` uses `md_udc`; `/misc` uses `para`. These mappings agree
between the stock first-stage ramdisk and the stock vendor fstab. The final
stock shipping API property is 33, following an earlier value of 31.

Stock virtual A/B depends on Android's downstream `dm-user` driver, which this
kernel does not have. This configuration instead reserves 4 GiB per physical
A/B slot inside the existing 8,792,064,000-byte super partition. It is a proposed
replacement layout, not an in-place upgrade of stock. Boot-control compatibility,
both slot transitions, recovery, encryption and OTA updates still require testing.
The stock record layout is now covered by LK instruction replay. The provisional
default boot HAL still lacks MediaTek boot-region selection and the successful
boot flag update; see `rabbit-r1/docs/BOOT-CONTROL.md` in the kernel repository.

Initial bring-up uses one CPU, permissive SELinux, retained firmware clocks and
regulators, and software graphics with the firmware framebuffer. The health HAL
reads real power supplies, but battery percentage and combined charging status
are unfinished. The existing kernel has no capacity property; the default HAL
can therefore report zero. Do not substitute a simulated battery to hide this.

Before distributing an Android image, complete the full Soong and SELinux
build, verify image sizes and the LK/DT handoff, integrate the bounded `expdb`
logger into Android init, and test boot and recovery on an r1. Storage, graphics,
battery health, charging, temperature limits, input, audio, wireless, cameras,
modem support and suspend remain hardware validation gates. Native display and
TCPC are still disabled in the staged DTB.

The patched LK and stock overlay are required with this kernel. Never relock
before restoring the complete stock firmware package, including every LK slot.
The current anti-relock patch blocks Fastboot's lock handler; it cannot prevent
another loader or a direct `seccfg` write from changing the lock state.
