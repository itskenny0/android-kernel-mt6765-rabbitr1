GE8320 firmware and device-information audit
==========================================

Verified 2026-10-09 for PowerVR GE8320, BVNC ``22.87.104.18``. The latest
official exact-BVNC firmware was available, but authoritative definitions
for its extra bitmap indices and a complete Mesa device table were not
found. This audit does not establish that the firmware is ready to run on
the r1. The GPU remains disabled. No firmware or external source is bundled.

``provenance.json`` records immutable source URLs, hashes, search scope and
attributed reports. ``numeric-coverage.json`` records the decoded values.
Original artifacts under ``/rabbitr1/out/`` are identified by absolute
workspace path and hash; they are not repository files or decoder dependencies.

Firmware and unresolved ABI
---------------------------

The official firmware branch tip was
``8a58f81883f7be458daa34e418cc4079f995b279`` (2026-04-15), version
``1.1.OS@6976702``, 118784 bytes, SHA-256
``8483f650788da9fee0e26ef43c2a97248579280a77db56a24aeb3bf815ca3c9b``.

`Exact firmware <https://gitlab.freedesktop.org/imagination/linux-firmware/-/blob/8a58f81883f7be458daa34e418cc4079f995b279/powervr/rogue_22.87.104.18_v1.fw>`_

The firmware sets BRN indices 26, 28 and 30, ERN index 8, and feature index
102 beyond the published mappings. Linux v7.2.9 warns and skips them; its
userspace quirk query cannot expose those skipped raw bits. Their meanings
and required handling remain unverified. Stock quirk-table ordering is not
evidence of their names. Neither public Imagination Linux branch,
``dev/bxs`` or ``powervr-next``, supplies the additional definitions.

The original ASan/UBSan host diagnostic exercised kernel validation and
metadata decoding. Its successful return retained three warnings; it did
not execute firmware or render. The Python decoder below reproduces the
metadata accounting, not that C diagnostic or full kernel validation.

Numeric coverage
----------------

All 22 parameters are consumed by known ABI fields. Known values include
19456 common-store dwords, seven ISP tiles in flight, ten partitions,
one cluster/raster pipe and 16x16 tiles. There is no unused parameter tail.
These 11 Mesa numeric properties lack corresponding published ABI fields
and remain without verified GE8320 values in this audit:

* ``max_instances_per_pds_task``, ``max_multisample``, ``max_usc_tasks``
* ``tpu_parallel_instances``, ``unified_store_depth``, ``usc_itr_parallel_instances``
* ``usc_slots``, ``uvs_banks``, ``uvs_pba_entries``, ``uvs_vtx_entries``, ``vdm_cam_size``

``fbcdc_algorithm`` and ``xpu_max_slaves`` are different: their ABI fields
exist but their feature bits are unset. Recorded flag coverage includes only
direct same-name matches; derived or renamed flags remain outside that
comparison. Unmatched names do not mean absent hardware features.

The previous official firmware, ``1.0.OS@6889268`` at commit
``79a956339d2251bfbca518936abc9610a1c3c197``, has identical masks and parameters.
Reverting to it would not fill these gaps.

Why GE8300 tables must not be copied
-----------------------------------

Mesa 26.2.4 and the vendor ``dev/devinfo`` tip recorded in the provenance
lack ``22.87.104.18``. Mesa selects its static device table before querying
runtime information; an unmatched BVNC fails initialization. The kernel
runtime query does not supply these missing limits. Published GE8300 values
already differ: 16384 common-store dwords and four ISP tiles versus GE8320's
19456 and seven. `Mesa's documentation <https://docs.mesa3d.org/drivers/powervr.html>`_
requires an exact BVNC match because features and hardware issues can vary.

Independent report, not our test: Dinolek wrote on 2025-08-15 that GE8320
firmware build 6734358 booted, but substituting GE8300 features, enhancements
and quirks produced a black screen and unknown FWCCB command ``2abc0069`` in
vkcubepp. This observation does not prove the failure's cause.

`Original discussion <https://gitlab.freedesktop.org/imagination/linux-firmware/-/issues/7>`_

The bounded local search covered ``/rabbitr1/src/kernel``, ``src/modules``,
``src/xiaomi`` and ``src/xiaomi-devs``. Exact-BVNC kernel-mode headers exist;
no UM configuration/core/feature-table header or reference to the 11 missing
numeric ``RGX_FEATURE`` names was found in those GPU C/header trees. This
does not establish that no licensed source exists elsewhere.

A static inspection of the stock GLES, Vulkan, services and USC compiler
libraries also established none of the eleven missing limits. All four are
stripped of full symbol and type information. Services exports
``RGXGetFeatureValue``, but the available headers do not define its signature
or selectors. The matching DDK's 24 kernel-mode selectors belong to a separate
accessor and do not name these limits. ``stock-user-metadata.json`` records
input hashes, observations and the bounded search scope; no vendor code was
executed, disassembled or imported.

Obtain license-permitted exact-BVNC user-mode definitions and the producer's
ABI enums, classify the extra bits, then implement required handling.
Actual device tests must establish firmware communication and rendering.
No speculative names, GE8300 clone or stub device table is supplied.

Reproduce the metadata accounting
--------------------------------

``decode-metadata.py`` uses Python's standard library and explicit input
paths. It validates every SHA-256 against adjacent ``provenance.json`` before
decoding. It performs no downloads or writes; JSON goes to standard output.
From the Linux checkout root, using separately obtained workspace inputs::

    source /rabbitr1/scripts/env.sh
    python3 rabbit-r1/tests/gpu/firmware/decode-metadata.py \
      --firmware /rabbitr1/out/gpu-review/firmware/rogue_22.87.104.18_v1.fw \
      --kernel-abi drivers/gpu/drm/imagination/pvr_rogue_fwif_dev_info.h \
      --kernel-mapping drivers/gpu/drm/imagination/pvr_device_info.c \
      --mesa-header /rabbitr1/out/gpu-review/mesa-update/src/imagination/common/pvr_device_info.h

Other locations work when input bytes match the recorded hashes. Immutable
upstream URLs are in ``provenance.json``; the complete kernel source manifest
is ``../powervr-7.2.9.json``. For the previous firmware, add
``--firmware-id previous`` and supply its file with ``--firmware``.

Both revisions reproduced the recorded masks, values and coverage. Wrong
firmware/header inputs were rejected with no JSON output. Decoder success
reports metadata interpretation only; unsupported indices remain unresolved.
