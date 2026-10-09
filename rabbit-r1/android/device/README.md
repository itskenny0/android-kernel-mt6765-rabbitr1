# haretic Android device configuration

This is haretic's initial LineageOS 24 product for the rabbit r1 mainline kernel in this
workspace. Android boot artifacts and targeted integration modules have passed
offline checks; a complete or hardware-tested Android ROM is not available.
The diagnostic kernel ZIP remains the assembled distribution package; Android
images have not been added to it.

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
The stock record layout is covered by LK instruction replay. The r1 boot HAL
implements MediaTek boot-region selection and the successful-boot flag update
with readback and failure handling. Its core has host tests; both Android
services and the policy aggregate now compile. Recovery policy and real system
`libbinder_ndk` builds also passed their artifact checks.
Full-image dependencies, linker namespace/APEX visibility and physical-device
checks remain. See `rabbit-r1/docs/BOOT-CONTROL.md`.

Initial bring-up uses one CPU, permissive SELinux, retained firmware clocks and
regulators, and software graphics with the firmware framebuffer. The health HAL
reads real power supplies. The gauge now reports the bound charger's actual
status, but battery percentage remains unfinished. The kernel has no capacity
property, so the default HAL reports zero capacity. Android's BatteryService
can still shut down because the battery is present and appears empty. This
is an Android boot blocker. Implement and validate real fuel-gauge reporting;
do not substitute a simulated battery or suppress the shutdown check.
The gauge now exposes the signed hardware charge counter in microamp-hours;
this is relative accumulation, not remaining capacity. Live profile-based
initialization, counter reset/rollover handling and SOC persistence remain.

The asynchronous `r1-expdb` service validates the partition and its bounded
18 MiB mapping before loading pstore. Its mocked discovery, mapping, attachment
and failure tests, Android build and compiled policy checks pass. Vendor-image
module installation, runtime labels, module attachment and persistence remain
unverified.

The [six integration targets](../targeted-build.json) and the subsequent
[boot/DTBO, recovery-policy and Binder targets](../boot-build.json) passed
offline artifact checks. As of 2026-10-09, the system, system_ext, product,
vendor, super and vbmeta build is in progress; it has not completed. No runtime
service test has completed. Before distributing an Android image, complete
those builds and their partition-size checks, verify super/vbmeta and vendor
module contents, validate the bounded `expdb` logger, and test the LK/DT handoff,
boot and recovery on an r1. Storage, graphics,
battery health, charging, temperature limits, input, audio, wireless, cameras,
modem support and suspend remain hardware validation gates. Native display and
TCPC are still disabled in the staged DTB.

The patched LK and stock overlay are required with this kernel. Never relock
before restoring the complete stock firmware package, including every LK slot.
The current anti-relock patch blocks Fastboot's lock handler; it cannot prevent
another loader or a direct `seccfg` write from changing the lock state.
