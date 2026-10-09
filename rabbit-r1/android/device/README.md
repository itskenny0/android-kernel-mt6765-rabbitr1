# haretic Android device configuration

This is haretic's initial LineageOS 24 product for the rabbit r1 mainline kernel in this
workspace. The default `lineage_r1` product at source `0a2a56a30e`, built with
`trunk_staging`, completed its full image build and offline artifact checks.
No hardware-tested Android ROM or complete flashing package is available.
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
The full image build and packaged service, library and allocator APEX checks
also passed. Runtime linker namespace/APEX visibility and physical-device
checks remain. See [boot control](../../docs/BOOT-CONTROL.md).

Initial bring-up uses one CPU, permissive SELinux, retained firmware clocks and
regulators, and software graphics with the firmware framebuffer. The health HAL
reads real power supplies. The default kernel leaves the experimental battery
model disabled, so missing capacity can still trigger Android's empty-battery
shutdown. The explicit `lineage_r1_soc` product requires a kernel built with
`build-mainline.sh --experimental-soc`; its Health service waits for genuine
initial capacity and requests shutdown if initialization fails. It retains real
zero and normal runtime shutdown behavior. The live stock-profile model and
charge-counter integration have offline tests, but are uncalibrated. Physical
validation, hidden counter resets and persistence remain open. The SOC Health
service, readiness helper and recovery service now pass their actual `cp2a`
product build and [offline artifact checks](../health-product-build.json),
including ARM64 linkage, installed init/VINTF, compiled split/recovery policy and
the SOC kernel/recovery Health files in boot. A subsequent inspection found
an inherited simulated Health provider and duplicate recovery Health services
in that configuration; the scoped checks did not verify uniqueness. The selector
now runs before common defaults and passes the inheritance regression; rebuilt images still need a uniqueness check.
Complete SOC filesystem images and physical startup remain unverified. See
[Android integration](../../docs/ANDROID.md) for selection and validation scope.

The asynchronous `r1-expdb` service validates the partition and its bounded
18 MiB mapping before loading pstore. Its mocked discovery, mapping, attachment
and failure tests, Android build and compiled policy checks pass. The default
product's completed vendor-image audit also verified module membership and
hashes against the kernel used for that build. Runtime labels, module attachment
and persistence remain unverified.

The [six integration targets](../targeted-build.json) and the subsequent
[boot/DTBO, recovery-policy and Binder targets](../boot-build.json) passed
offline artifact checks. The [full-image audit](../full-image-build.json)
verified system, system_ext, product, vendor, super and vbmeta, partition-size
checks and packaged contents. A separate [dual-slot super build](../dual-slot-super-build.json)
verified both logical slots contain the audited images. These results cover
the default product's `trunk_staging` build, with experimental SOC Health off.

`scripts/build-android.sh` now defaults to the finalized Android 17 `cp2a`
release configuration. Set `R1_ANDROID_RELEASE=trunk_staging` to reproduce the
earlier development configuration. The explicit `lineage_r1_soc` product has
passed its `cp2a` Health, policy, boot and DTBO targets and the scoped checks
above. Complete `cp2a` filesystem images still need their own build and audit;
the earlier full-image results do not cover that change. No runtime service test
has completed. Before distributing an Android image, finish the complete flashing
package, validate the bounded `expdb` logger, and test the LK/DT handoff, boot
and recovery on an r1. Storage, graphics,
battery health, charging, temperature limits, input, audio, wireless, cameras,
modem support and suspend remain hardware validation gates. Native display and
TCPC are still disabled in the staged DTB.

The patched LK and stock overlay are required with this kernel. Never relock
before restoring the complete stock firmware package, including every LK slot.
The current anti-relock patch blocks Fastboot's lock handler; it cannot prevent
another loader or a direct `seccfg` write from changing the lock state.
