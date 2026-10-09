# LineageOS 24 build integration

The initial r1 product is tracked under `android/device`; the charging UI and
service remain under `android/charging`. The six integration targets, including
native services and the SELinux policy aggregate, built successfully on
2026-10-09. Artifact checks passed for the ARM64 binaries, charging APK,
init/VINTF files and compiled policy. [Results and hashes](../android/targeted-build.json)
record their scope. The subsequent boot, DTBO, recovery-policy and real Binder
runtime targets also built; [boot-image evidence](../android/boot-build.json)
records their artifact checks. Complete filesystem images and hardware testing
remain outstanding. No Android image has been added to the mtkclient package.

The updated recovery log collector, dumpstate, system/recovery liblog and ARM
graphics allocator also built and passed [artifact checks](../android/pstore-graphics-build.json).
Both liblog variants export the private backend reader, recovery-persist imports
it, and the boot ramdisk contains the checked recovery library. The allocator
APEX contains 20 AArch64 binaries with the expected service, mapper and VINTF
declarations. Its external library declarations match the ELF imports; runtime
APEX/linker behavior still needs testing.

`android/local_manifests/r1.xml` pins the common mainline device tree and its ten
direct and transitive dependencies. Use it with the LineageOS manifest commit in `sources.lock.json`.
The upstream manifest uses the Android 17 release tag for AOSP projects and
moving Lineage branches for other projects; record a resolved manifest after
sync and account for any local source edits when recording build provenance.

The current workspace has an initialized checkout at `/rabbitr1/src/android`.
The repo launcher is `/rabbitr1/toolchains/git-repo/repo`. Keep its configuration
in `/rabbitr1/.cache/repo` and its user Git configuration in the workspace's XDG
configuration directory. Do not move caches or build output outside `/rabbitr1`.

After building and checking the diagnostic kernel and mtkclient package:

```bash
source /rabbitr1/scripts/env.sh
export REPO_CONFIG_DIR=/rabbitr1/.cache/repo REPO_NO_INTERACTIVE=1
mkdir -p /rabbitr1/src/android/.repo/local_manifests
cp /rabbitr1/android/local_manifests/r1.xml /rabbitr1/src/android/.repo/local_manifests/r1.xml
cd /rabbitr1/src/android
python3 /rabbitr1/toolchains/git-repo/repo sync -c -j6 --no-tags --no-clone-bundle
python3 /rabbitr1/toolchains/git-repo/repo manifest -r -o /rabbitr1/out/lineage-resolved.xml
python3 /rabbitr1/scripts/install-lineage-device.py
```

The installer stages verified build inputs, including the stock overlay that LK
still uses for its private vendor tree. It changes only the mainline DTB's eMMC
status; the other device gates are retained. It does not flash a device or place
patched LK into an Android OTA.

Once sync completes, run the actual product configuration and targeted build:

```bash
bash /rabbitr1/scripts/build-android.sh
```

The helper checks and applies the pinned Android compatibility patches, stages
the device files and kernel, records the resolved manifest, selects the product
and builds charging controls, the expdb logger, both boot services and policy.
It defaults to eight jobs (`R1_ANDROID_JOBS` overrides this) and accepts explicit
Android build targets as arguments. Logs remain under `/rabbitr1/logs`.

The additional boot build includes the separate `sepolicy.recovery` target and
the real system `libbinder_ndk` implementation. Static dependency inventories
found the required libraries for recovery and normal services; Android linker
namespace/APEX visibility and runtime operation still need validation. The APK
has a valid platform test signature and 16 KiB-aligned, uncompressed ARM64 JNI
libraries. Compiled recovery policy includes the boot service's rootfs execution
and domain-transition permissions. Current boot arguments still request global
permissive mode.

The boot image retains the stock v2 header, load addresses and Android DT table.
Its normal-boot fstab must be copied to `root/first_stage_ramdisk`, which the
recovery-as-boot rule includes in its CPIO. The stock DTBO payload is unchanged;
Android replaces its AVB footer. Both boot and DTBO footers use algorithm `NONE`
with valid hash descriptors, and do not establish a complete or trusted vbmeta
chain. Vendor module installation, super metadata, complete partition sizes and
top-level vbmeta remain to be checked after the filesystem build. The stock layout
evidence is recorded in [stock-android-layout.json](research/stock-android-layout.json).

The recovery-as-boot image relies on LK to select normal Android startup.
Bounded instruction replays of both stock and patched LK confirm that normal
mode appends `androidboot.force_normal_boot=1`, while recovery mode omits it.
[Mode-handoff evidence](../android/lk-mode-handoff.json) records the binary and
code hashes and four replay cases. This does not establish a physical boot.
Do not add the token unconditionally to the image header: recovery needs its
own ramdisk path.

After the boot and recovery checks pass, build the filesystem images and their
AVB/super dependencies with the explicit size check:

```bash
R1_ANDROID_JOBS=24 bash /rabbitr1/scripts/build-android.sh \
    systemimage systemextimage productimage vendorimage \
    superimage vbmetaimage check-all-partition-sizes
```

These targets exclude the 48 GiB userdata image. The configured super partition
allows a 4 GiB group per slot, including filesystem and AVB overhead; actual
image fit still needs the size check. Factory super populates only slot A and
leaves B's logical partitions empty. The future Android flash procedure must
select and verify A while preserving the previous slot state for restoration,
or supply populated B images. A device's existing active slot cannot be assumed.

The first full-image attempt failed when minigbm's default `all` backend set
compiled Intel intrinsics for ARM64. The r1 product now selects the existing
`all_arm` set before inheriting the common device configuration. The rebuilt
allocator passes: its actual compile commands exclude i915/xe and retain the
ARM and generic backends. The complete filesystem build must still finish.

The helper uses `OUT_DIR=out` and creates `/rabbitr1/src/android/out` as a link
to `/rabbitr1/out/android`. It preserves an existing path that points elsewhere
by stopping before the build. The pinned Siso tool cannot load an absolute
config directory, while Soong test packaging rejects paths containing `..`.
The source-relative alias satisfies both and keeps the cache in the workspace.

The pinned vendor image libraries need vendor builds of several dependencies.
The compatibility patches enable the required library variants. Patch
application checks repository commits, patch hashes and exact touched-file
contents before changing anything; it accepts its already-applied changes and
refuses unrecognized edits. A resolved manifest identifies repository commits;
the patch series records the intentional differences from those commits.
Unrelated local edits are preserved; review and record them separately before
a release build. New text files require pinned absence in the base and index,
a fixed resulting hash, and a regular non-executable file; conflicting local
files are preserved. The helper completes all project checks before applying
any patch.

The graphics sources include the complete [Mesa 26.2.4 release](https://docs.mesa3d.org/relnotes/26.2.4.html)
delta on the pinned Lineage tree. Its six Android/integration differences remain
byte-for-byte unchanged. [Source provenance](../android/patches/mesa-26.2.4.json)
records the upstream revisions, release checksum and retained delta. Source
application and repeat checks passed. The PowerVR Vulkan library also passed
a separate x86_64 Linux host build; Android/ARM64 userspace compilation remains
pending. [Compile evidence](../tests/gpu/mesa-host-build.json) records its scope.
A separate ARM64/bionic attempt reached a direct-display backend that requires
unsupported pthread cancellation. The [failed build record](../tests/gpu/mesa-android-build-attempt.json)
pins its inputs; the next attempt needs Mesa's Android platform configuration
and its remaining vendor library dependencies. No GPU library from this attempt
is installed in the product.

The kernel PowerVR driver uses the complete Linux 7.2.9 subtree, including
its unchanged UAPI, plus the upstream scheduler timeout fix in patch 0075.
The fix gives firmware recovery time to finish before the host timeout callback;
it also delays host diagnosis when firmware fails to recover. A separate CI
configuration enables the driver, debugfs
and tracing and compiles all 28 ARM64 objects with `W=1`; warnings fail the
check. [Driver pins and compile scope](../tests/gpu/README.rst) record the
source and validation. The shipping configuration keeps PowerVR disabled.

The r1 GPU is PowerVR Rogue GE8320, BVNC `22.87.104.18`. This revision is absent
from Mesa 26.2.4's device table and the kernel's documented supported GPU list.
Matching upstream firmware exists, but the current kernel reports unhandled
firmware feature and errata bits. Mesa also lacks the required Android native
buffer/AHardwareBuffer integration for this driver, and the r1 GPU board wiring
is not implemented. The Android product continues to use SwiftShader. Updating
Mesa does not establish working hardware acceleration.

The [device notes](../android/device/README.md) track the current hardware and
release gates, including missing battery capacity (which can trigger Android's
empty-battery shutdown), Android `expdb` validation, and
the change from stock virtual A/B to dedicated A/B extents. Existing flash and
restore helpers cover the diagnostic package; an Android package needs its own
complete backup, layout and restore validation before distribution.

The [boot-control adapter](BOOT-CONTROL.md) implements the stock A/B record
format, eMMC boot-region selection and clearing the `avbbctl` flag. Its core
and storage boundary have host tests. Both services pass their Android build,
artifact and compiled recovery-policy checks. Runtime dependencies and device
operation remain to be checked on the complete system.

The `r1-expdb` service starts asynchronously after `post-fs` when the fixed build
property `ro.vendor.r1.expdb.enabled=1` is set. It verifies the 20 MiB `expdb`
partition and creates a UUID-owned, single-target mapping of its first 18 MiB.
It checks the active table, device identities and ueventd links before loading
the two fixed pstore modules, then verifies backend registration and parameters.
The final 2 MiB is excluded. The diagnostic mapper shares the same geometry.
The service never formats or erases the partition. Failed setup leaves logging
unavailable and reports the error to the kernel log; Android boot can continue.

The logger's host tests simulate device syscalls and module loading. Run them
with `python3 android/device/logging/tests/run.py`. The kernel's pstore callbacks
use the retained opener credentials so readers, console callers and background
workers do not need raw block-device permissions. Combined policy compilation
passed; actual node labels, module attachment and persistence still need validation.
Best-effort pstore has no dedicated panic writer, so neither panic persistence
nor logs from before startup are guaranteed.

Recovered files use the `pstore_blk` backend name, for example
`/sys/fs/pstore/console-pstore_blk-0`. The Android logging patch lets `logcat -L`
and dumpstate's LAST LOGCAT read `pmsg-pstore_blk-0` when `pmsg-ramoops-0` is
absent. It selects one backend and does not merge unrelated boot histories.
The writer retries a missing `/dev/pmsg0` once per second, so starting logd
before the expdb backend no longer permanently disables pmsg writes. Earlier
messages are not replayed; the 64 KiB pmsg ring retains only a rolling tail.

Both full reader/writer sources pass host sanitizer tests, including delayed
availability and concurrent writers, and compiled in the Android build. Reboot
persistence remains unverified. Kernel console records still require direct collection or
the expdb decoder; Android's separate LAST KMSG and recovery/erase paths retain
their ramoops filenames. See [the logging tests](../tests/pmsg/README.md) for
the access checks and remaining limitations.
