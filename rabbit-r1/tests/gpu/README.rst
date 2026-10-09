PowerVR source and compile check
===============================

The inactive PowerVR driver uses the complete Linux v7.2.9 subtree from
stable commit ``5fce161649b4d779d1b76d9fcd52dc77779774b8``. Patch 0072
imports 20 changed or new files while preserving upstream source bytes.
The remaining driver files and ``include/uapi/drm/pvr_drm.h`` already matched.
``powervr-7.2.9.json`` records SHA-256 and Git blob hashes for all 78 driver
files and the unchanged UAPI header. Original upstream licenses remain intact.

The October 9 upstream review found Mesa 26.2.4 and Linux 7.2.9 still the latest
stable releases. ``upstream-status.json`` records the checked development tips,
source comparisons and remaining exact-device support gaps. No additional
stable PowerVR fix was found. Mesa main has newer compiler and shared-API work;
the existing pins do not claim to include every development change. The r1's
exact BVNC is still absent from Mesa main, so production continues to use
SwiftShader. This refresh adds source evidence, not a rendering test.

Patch 0075 adds upstream commit
``2224d6642136767ec01d146d48ffc881b55b32f7``: the scheduler timeout increases
from 500 ms to 60 seconds, twice the existing 30-second firmware HWR deadline.
This gives firmware recovery time to run before the host timeout callback;
it also delays host timeout diagnosis if firmware does not recover. The
callback and firmware deadline are unchanged. ``powervr-backports.json`` pins
the patch and its before/after source hashes separately from the unchanged
79-file baseline manifest. This is a focused backport, not a full RC import.

Run the check after installing the workspace tools::

    source /rabbitr1/scripts/env.sh
    /rabbitr1/scripts/compile-powervr.sh

The canonical script also works directly from
``/rabbitr1/src/mainline/rabbit-r1/scripts/compile-powervr.sh``. It uses the
existing ARM64 GCC cross compiler and only writes to
``/rabbitr1/out/powervr-check`` plus the workspace paths set by ``env.sh``.
It never uses ``out/mainline`` or edits the production defconfig.

The check validates the backport patch hashes and ordered before/after hash
chain, then verifies all 79 source files against the resulting pins. It
generates ``rabbit_r1_defconfig`` in its
own output directory, enables PowerVR, debugfs and tracing there, runs
``olddefconfig`` and ``prepare``, and compiles the driver with ``W=1``.
Kbuild cleans this driver's scratch objects before each compile so repeated
runs still check all 28 objects. The resulting archive must contain the
expected objects, including tracepoints and debugfs. Warnings fail the check.
``build.log`` and ``audit.json`` record the commands, source/config hashes,
compiler, object list, applied backport records and result. Source and patch
hashes are checked again after compilation. Updating the driver later requires
reviewing the base manifest or adding an explicit backport record; this is a
pin, not a hardware feature test.

The Linux v7.2.9 baseline passed this check on ARM64 with GCC 13.3.0. The driver
uses the existing kernel 7.1 DRM/scheduler APIs without compatibility changes.
This checks source composition and compilation, not full kernel linking,
firmware operation or rendering. No synthetic tests of upstream algorithms
are substituted for hardware validation.
The timeout backport also passed all 28 ARM64 objects with ``W=1`` and no
warnings. ``powervr-timeout-build.json`` records the exact source pins, compiler
and logs. This run used the pending MT6357 diagnostic config option but did
not compile the gauge. The later full-link check below includes both changes.

Additional compile results
--------------------------

A full AArch64 kernel link passed with PowerVR, debugfs and tracing enabled
in the separate configuration at kernel commit
``9ab2d3454215448b413014946061b673a1967a6b``. It produced no compiler warnings
or undefined symbols; the linked image contains the PowerVR probe, init and
tracepoints. ``kernel-full-link.json`` records the config, image and log hashes.
After the subtree check above, repeat this optional link check with::

    make -C /rabbitr1/src/mainline O=/rabbitr1/out/powervr-check \
      ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j"$JOBS" vmlinux

CI runs the 28-object subtree check. The full-link artifact remains in its
separate output directory and is not included in the flash package.

A subsequent full ARM64 ``W=1`` build at
``83cbd9b71f3c7e8248eea9dddf0af34e843416fb`` includes the scheduler fix and
MT6357 live collector. ``kernel-full-link-0074-0075.json`` records the linked
kernel, Image and ten modules, with the required gauge/PowerVR symbols and
no undefined kernel symbols. It produced 1,071 warnings in 16 unchanged
base-kernel files, with none in the gauge, IIO, device core or PowerVR.
Compiled inputs and config stayed unchanged. The initial broader source
guard also covered a concurrently updated CI workflow; its mismatch is
preserved separately from the successful build and compiled-input check.

Mesa 26.2.4's PowerVR Vulkan shared library also compiled and linked through
613 Ninja actions, with zero Mesa compiler warnings. This was an x86_64
Linux/glibc, headless build of the DRM backend. It neither validates Android
or ARM64 userspace nor tests rendering. No driver was loaded or GPU opened.
``mesa-host-build.json`` records source revisions, Meson options, tool hashes,
dependency versions and the output hash. Its private libdrm build produced
three existing ignored-asprintf compiler warnings, retained in the log.

The native build used the pinned Lineage Mesa base plus patch 0005, a private
libdrm 2.4.124 build, the existing mesa-build-dep generators and a source-built
Mesa 26.2.4 ``pco_clc``. The existing bundled generator produced identical
C/header output for the default device list on the actual PowerVR SPIR-V
input. This comparison does not cover other devices or shader correctness.

The JSON files name original workspace logs and a reproduction script under
``/rabbitr1/out/``; those artifacts are not bundled in the repository.

An ARM64/bionic build attempt used the platform compiler, Android API 37 headers
and vendor link interfaces. It stopped in ``wsi_common_display.c`` because that
direct-display backend requires pthread cancellation, which bionic does not
implement. Mesa enables that backend with ``platforms=[]`` even for an Android
target. No shared library was produced, and no cancellation shim or Mesa source
patch was added. ``mesa-android-build-attempt.json`` records the failed build
and its verified inputs; this is distinct from the successful Linux host build.

The subsequent ``platforms=android`` build compiled and linked against the
actual ARM64 vendor dependencies and passed a private Meson installation.
``mesa-android-platform-build.json`` records its source and input hashes,
retained warnings, failure history and verified artifacts. The installed
library has no RPATH/RUNPATH and exports Android's ``HMI``. No target code was
executed, and the private install prefix is not Android product packaging.

Two dependency corrections from that build are now in patch 0005: libui
exports for imapper5, and libdrm plus conditional Android exports for PowerVR's
per-architecture target. All 118 declared outputs match a fresh patch replay.
The earlier 116-file ``android-library-mapping.json`` remains the separate
Make check for ``imagination`` mapping to ``libvulkan_powervr_mesa.so`` and
``vulkan.powervr_mesa.so``. That Android.mk mapping was not in the private
Meson source snapshot and was not an input to its compilation. Image build
``6555dd117c`` retains the earlier patch and does not validate the mapping.
Android buffer-sharing support and hardware tests remain necessary.
Production uses SwiftShader.

The r1 GPU remains disabled
-------------------------

The stock GPU is PowerVR GE8320, BVNC ``22.87.104.18``. Linux v7.2.9 still
classifies it as unknown, and Mesa 26.2.4 lacks its device table entry.
An exact-BVNC mainline firmware exists, but the production decoder reports
unsupported BRN bits 26, 28 and 30, ERN bit 8, and feature bit 102. The
firmware decoder and those warnings are unchanged by this driver update.
Their meanings and required workarounds remain unresolved.
The `firmware audit <firmware/README.rst>`_ records source pins, numeric
coverage and a reproducible metadata decoder.

Patch 0072 supplies upstream query, bounds, MMU, context cleanup, paired-job
and tracing improvements. It does not enable the GPU, add a DT node or
firmware package, bypass the supported-device gate, or switch Android away
from SwiftShader. Actual r1 power/clock/IRQ integration, device support,
Android buffer sharing and rendering tests remain necessary.

Primary sources
---------------

* `Linux v7.2.9 source <https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/drivers/gpu/drm/imagination?h=v7.2.9>`_
* `Upstream scheduler timeout fix <https://github.com/torvalds/linux/commit/2224d6642136767ec01d146d48ffc881b55b32f7>`_
* `PowerVR kernel documentation <https://docs.kernel.org/gpu/imagination/index.html>`_
* `Mesa PowerVR documentation <https://docs.mesa3d.org/drivers/powervr.html>`_
* `Exact GE8320 firmware <https://gitlab.freedesktop.org/imagination/linux-firmware/-/blob/8a58f81883f7be458daa34e418cc4079f995b279/powervr/rogue_22.87.104.18_v1.fw>`_
