PowerVR source and compile check
===============================

The inactive PowerVR driver matches the complete Linux v7.2.9 subtree from
stable commit ``5fce161649b4d779d1b76d9fcd52dc77779774b8``. Patch 0072
imports 20 changed or new files while preserving upstream source bytes.
The remaining driver files and ``include/uapi/drm/pvr_drm.h`` already matched.
``powervr-7.2.9.json`` records SHA-256 and Git blob hashes for all 78 driver
files and the unchanged UAPI header. Original upstream licenses remain intact.

Run the check after installing the workspace tools::

    source /rabbitr1/scripts/env.sh
    /rabbitr1/scripts/compile-powervr.sh

The canonical script also works directly from
``/rabbitr1/src/mainline/rabbit-r1/scripts/compile-powervr.sh``. It uses the
existing ARM64 GCC cross compiler and only writes to
``/rabbitr1/out/powervr-check`` plus the workspace paths set by ``env.sh``.
It never uses ``out/mainline`` or edits the production defconfig.

The check verifies source hashes, generates ``rabbit_r1_defconfig`` in its
own output directory, enables PowerVR, debugfs and tracing there, runs
``olddefconfig`` and ``prepare``, and compiles the driver with ``W=1``.
Kbuild cleans this driver's scratch objects before each compile so repeated
runs still check all 28 objects. The resulting archive must contain the
expected objects, including tracepoints and debugfs. Warnings fail the check.
``build.log`` and ``audit.json`` record the commands, source/config hashes,
compiler, object list and result. Updating the driver later requires reviewing
and updating the source manifest; this is a pin, not a hardware feature test.

The canonical driver passed this check on ARM64 with GCC 13.3.0. The driver
uses the existing kernel 7.1 DRM/scheduler APIs without compatibility changes.
This checks source composition and compilation, not full kernel linking,
firmware operation or rendering. No synthetic tests of upstream algorithms
are substituted for hardware validation.

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
* `PowerVR kernel documentation <https://docs.kernel.org/gpu/imagination/index.html>`_
* `Mesa PowerVR documentation <https://docs.mesa3d.org/drivers/powervr.html>`_
* `Exact GE8320 firmware <https://gitlab.freedesktop.org/imagination/linux-firmware/-/blob/8a58f81883f7be458daa34e418cc4079f995b279/powervr/rogue_22.87.104.18_v1.fw>`_
