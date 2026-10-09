# Rabbit r1 boot control

The stock r1 uses the AOSP 32-byte `bootloader_control` record at byte 2048 of
`/misc`. Stock Android maps `/misc` to the GPT partition `para`; LK's partition
API calls it `misc`. The Lineage product selects the r1 adapter under
`android/device/boot` for Android and recovery. Its core and storage boundary
are host-tested. The AIDL wrapper passes a host syntax check against the generated
Android interfaces; target linking, Soong, SELinux and device validation are pending.

The adapter implements two MediaTek extensions: activating a slot also selects
an eMMC boot region, and marking a boot successful clears a vendor flag. These
findings come from the actual v0.8.293 binaries, compared with the synced AOSP
boot HAL and the mainline MMC driver. They do not establish that an OTA, slot
transition or interrupted write works on hardware.

## Evidence

The stock firmware archive is the official
[v0.8.293 release](https://github.com/rabbit-hmi-oss/firmware/releases/download/v0.8.293/rabbit_OS_v0.8.293.zip).
Its SHA-256 is
`f6c28b221a91055ec5e44ab0ac0ee59c7e6b52fac12ad08f2bb23c8f0551c6c0`.

| Input | SHA-256 |
| --- | --- |
| `lk.img` | `534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e` |
| `vendor/lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so` | `a760f28732f9bd16c734bb09ed1393c9dd9d5e6c886b8400f1dc2f0a41dd722a` |

The LK instruction addresses below are file offsets. Add `0x47fffe00` to obtain
the loaded address. HAL addresses are ELF virtual addresses before relocation.

| Operation | LK offset | HAL address |
| --- | --- | --- |
| Read/write A/B record | `0x3da9c` | `0xc3c0` / `0xc5f0` |
| Select highest-priority slot | `0x3dce4` | `0xd598` |
| Activate slot | `0x3dd8c` | `0xd638` (core), `0xb3e8` (wrapper) |
| Mark boot successful | — | `0xd4f8` (core), `0xb2f8` (wrapper) |
| Set eMMC boot region | `0x531d0` | `0xbd20` (includes UFS paths) |
| Clear `avbbctl` | — | `0xb098` |

Both read/write implementations use 32 bytes at offset 2048. The magic is
`0x42414342`, version 1, with two slots. Slot entries start at byte 12 and occupy
two bytes each. Their priority, retry count, successful-boot bit and verity bit
match `hardware/interfaces/boot/1.1/default/boot_control/include/private/boot_control_definition.h`.
The last four bytes contain the little-endian CRC-32 of the preceding 28 bytes.
LK writes this CRC, although its inspected read helper accepts a valid magic
without checking the CRC. That limitation is recorded in the replay test.

LK's activation helper sets priority 15, seven tries and clears the target's
successful bit. The Android HAL core sets priority 15 and six tries while
preserving that bit, matching AOSP. Both reduce another slot's priority from
15 to 14. The LK selector prefers slot A on equal priority; separate helpers
read retry and successful status. The replay does not cover the whole boot
loop's rollback decisions.

## MediaTek extensions

The HAL's `setActiveBootSlot` wrapper writes the A/B record, then calls
`SetBootRegionSlot`. The eMMC path reads `EXT_CSD[179]` with `MMC_IOC_CMD`
`SEND_EXT_CSD` and changes only bits 5:3 using `SWITCH`. Android slot A maps to
boot-region value 1, and B to value 2. These are the hardware boot regions,
separate from the GPT `boot_a` and `boot_b` partitions. All other bits, including
boot acknowledgment and partition access, are preserved. If the selected value
already matches, it does not issue a switch command. The binary probes UFS
first; the r1 adapter only needs its verified eMMC path.

LK's activation helper makes the same boot-region selection before writing the
A/B record. Consequently the two stock interfaces have different partial
failure states: LK can switch the boot region and then fail to write metadata;
Android can write metadata and then fail to switch the boot region. Neither
observed ordering is evidence of a transaction across those two storage areas.

After a successful core `MarkBootSuccessful`, the stock wrapper calls
`clearAvbbctlFlag`. That function reads the record, validates its CRC, and clears
byte 20 (`reserved1[0]`) only when it equals 1. It then recomputes the CRC and
writes the record. Other reserved bytes are preserved. The stock helper does
not propagate its write failure through the HIDL result; an adapter should
report a failure and allow retry rather than copy that behavior.

The mainline kernel already exposes `MMC_IOC_CMD` and updates its cached
`part_config` after a successful `EXT_CSD_PART_CONFIG` switch in
`drivers/mmc/core/block.c`. No downstream MediaTek ioctl is needed for this
specific operation. The adapter's init configuration supplies `SYS_RAWIO`.
Its device policy grants `MMC_IOC_CMD` for the whole-disk node and `BLKGETSIZE64`
for metadata size checks. Platform rules add common block-device ioctls;
combined policy compilation remains pending.

## Android adapter

The service validates the record's magic, version, slot count and CRC before
each operation. Invalid metadata is reported without replacing it with defaults.
Marking a boot successful updates the success/retry fields and clears `avbbctl`
in one 32-byte write. Writes are synchronized and checked by an aligned 4096-byte
`O_DIRECT` read of the partition's first block, bypassing the kernel's page cache.
Short direct reads fail without an unaligned retry. All other `para` bytes remain
outside the write range. Readback verifies the device's reported contents; reboot
and power-loss persistence still need hardware tests. Merge status uses AOSP's separate
standard misc message and helpers.

The Linux backend verifies the `para` GPT name and 512 KiB size, then identifies
its parent MMC disk using sysfs and matches that disk's device number. It opens
the whole-disk fd read-only and uses the mainline kernel's `MMC_IOC_CMD` interface
with `SYS_RAWIO`. The service never issues ordinary writes to that fd. The
record fd has an advisory lock, and Binder operations are serialized.

Activation first validates the existing eMMC selection (boot region 1 or 2),
then writes the new metadata and changes the selector. A metadata write failure
triggers restoration before any selector change. If a selector change cannot be
verified, the adapter forces a restoring command and verifies it before restoring
old metadata. Failed restoration is reported explicitly; the adapter does not
claim that the device is in its original state. These writes are not atomic
across power loss. Usable preloaders in both hardware boot regions remain a
prerequisite for device testing.

## Offline checks

Run after extracting the verified stock firmware and building patched LK:

```bash
source /rabbitr1/scripts/env.sh
toolchains/boot-tools/bin/python scripts/test-lk-boot-control.py
toolchains/boot-tools/bin/python scripts/test-stock-boot-hal.py
python3 scripts/test-r1-boot-control.py
```

`test-lk-boot-control.py` runs 2,057 cases each against stock and patched LK.
It executes the shipped selection, mutation, copy and CRC instructions.
Partition I/O, device discovery, logging and the eMMC selector are fixtures.
It checks all priority pairs, retry and successful fields, both activation
directions, reserved-byte preservation, CRC output, bounded writes, invalid
suffixes and read/write/selector failures. The kernel workflow runs this test.

`test-stock-boot-hal.py` requires the verified vendor library at
`research/android/stock-vendor/lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so`.
The file was extracted read-only from `research/android/vendor_a.img` using
the locally built `out/e2fsprogs/debugfs/debugfs`:

```bash
out/e2fsprogs/debugfs/debugfs -R 'dump /lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so /rabbitr1/research/android/stock-vendor/lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so' research/android/vendor_a.img
```

The HAL test runs 3,123 cases through the actual metadata mutations, slot queries,
flag clearing, CRC and boot-region logic. Its fixtures provide record storage, libc calls and eMMC
ioctls. Every possible original partition-configuration byte is checked for
both slots, including preservation of unrelated bits and the no-change case.
Query coverage includes both current slots, all priority pairs and all entry bytes.
The tests write JSON results under `out`; neither test touches a block device.
The HAL extraction and replay are local checks, not yet part of CI.

`test-r1-boot-control.py` compiles the production core and Linux backend with
AddressSanitizer and UndefinedBehaviorSanitizer. The core is compared against
80 committed record transformations captured from the stock HAL instructions,
then tested for corruption, queries and partial I/O failures. The backend tests
wrap its actual pread/pwrite/fsync/ioctl calls to check bounds, short transfers,
interruptions, synchronization failures, readback mismatches and command fields.
Separate cache and media fixtures check dropped and corrupted device writes.
Discovery tests use real sysfs text fixtures and simulated device syscalls to
check GPT identity, size, parent disk, descriptor replacement, locking and cleanup.
They do not open a host block device or validate real enumeration, Binder or policy.
CI runs these tests without needing the stock vendor image. Regenerate the oracle
from the verified extracted library with:

```bash
toolchains/boot-tools/bin/python scripts/generate-stock-boot-vectors.py /rabbitr1/src/mainline/rabbit-r1/tests/boot-control/stock-vectors.txt
```

## Remaining integration

Build both r1 Android boot services and the combined policy. Confirm sysfs and
block-device labels and the selected eMMC path against the board's actual
enumeration, and verify both boot regions contain usable preloaders before
enabling slot changes. Verify physical readback and persistence of the metadata
and selector. Exercise Android recovery and update_engine callers,
failed writes, failed switches, retries, reboot and interrupted operations.
Physical boot, rollback and recovery testing remain release gates.
