# Rabbit r1 boot control

The stock r1 uses the AOSP 32-byte `bootloader_control` record at byte 2048 of
`/misc`. Stock Android maps `/misc` to the GPT partition `para`; LK's partition
API calls it `misc`. The initial Lineage product has the correct physical
mapping, but its default boot HAL is incomplete for this device.

Two MediaTek extensions need an Android adapter: activating a slot also selects
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
specific operation. The Android adapter still needs narrowly scoped device
access, the required raw-I/O capability, and matching SELinux ioctl policy.

## Offline checks

Run after extracting the verified stock firmware and building patched LK:

```bash
source /rabbitr1/scripts/env.sh
toolchains/boot-tools/bin/python scripts/test-lk-boot-control.py
toolchains/boot-tools/bin/python scripts/test-stock-boot-hal.py
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

The HAL test executes the actual metadata mutations, flag clearing, CRC and
boot-region logic. Its fixtures provide record storage, libc calls and eMMC
ioctls. Every possible original partition-configuration byte is checked for
both slots, including preservation of unrelated bits and the no-change case.
The tests write JSON results under `out`; neither test touches a block device.
The HAL extraction and replay are local checks, not yet part of CI.

## Remaining integration

Implement and build the r1 Android boot HAL with both extensions and explicit
failure handling. Confirm the selected eMMC path against the board's actual
enumeration, and verify both boot regions contain usable preloaders before
enabling slot changes. Validate resulting metadata with readback and retain
all unrelated `para` bytes. Exercise Android recovery and update_engine callers,
failed writes, failed switches, retries, reboot and interrupted operations.
Physical boot, rollback and recovery testing remain release gates.
