# MT6357 boot measurements

Patch 0073 preserves the first MT6357 parent observation before PMIC children can
reset AUXADC or consume boot state. This is read-only input acquisition. It does
not publish CAPACITY or VOLTAGE_OCV, select an SOC seed, clear a ready flag, reset
the PMIC, change charging, or substitute a live voltage for missing boot data.

## Capture and interpretation

`drivers/mfd/mt6397-core.c` first reads the model's chip ID through the existing
probe path. Only the MT6357 match data with chip ID 0x57 enables the optional
capture. Identity errors follow the existing probe failure path. Other models
perform no optional telemetry reads.

The capture reads each physical 16-bit register exactly once:

| Snapshot field | Register | Stock interpretation |
| --- | --- | --- |
| POWER_ON | 0x10ac | bit15 ready, bits14:0 ADC code |
| PLUGIN | 0x10c0 | bit15 ready, bits14:0 battery-plugin code |
| STARTUP | 0x0a20 | startup selector bit2 |
| SYSTEM_INFO | 0x0d9a | boot/charging flags and retained whole SOC |
| BATTERY_STATUS | 0x0e08 | battery absent when bit1 is set |

Each field retains its transport error. A failed read leaves raw data zero and
invalid; the error must be checked before interpretation. The capture continues
after a field failure, and optional telemetry failures do not prevent unrelated
PMIC children from registering. There is no retry or later replacement of the
first observation. The boottime interval brackets these five reads; it is not
the PMIC capture time and does not imply atomicity between registers.

The snapshot is complete before IRQ initialization and the actual
`devm_mfd_add_devices()` call. This matters because the existing MT6357 AUXADC
probe asserts/releases reset before IIO registration. Retention of boot captures
across that reset has not been verified. This patch leaves reset behavior alone.
The new code performs no PMIC writes, ready clears, latch operations or active
ADC conversions. One diagnostic line reports raw/errno pairs and ready values:
-1 means the ready bit could not be read, 0 means clear, 1 means set. It assigns no
voltage validity or percentage.

`include/linux/mfd/mt6357/boot.h` supplies pure helpers:

- `mt6357_boot_decode_ocv()` propagates transport errors and returns `-ENODATA`
  if ready is clear, leaving output unchanged. Otherwise it decodes the raw
  code as `floor(code * 54000 / 32768) * 100` microvolts, preserving the stock
  0.1 mV truncation. Success means ready data was scaled, not that the result is
  fresh, electrically plausible, or a valid OCV/SOC seed.
- `mt6357_boot_decode_metadata()` requires an observed snapshot and successful
  metadata reads before publishing any output. It returns the startup selector,
  CON0 bits 0..5, the raw seven-bit stored SOC, and battery presence. Stored SOC
  values 101..127 remain raw metadata; there is no clamping or validity inference.
- `mt6357_boot_parse_lk_decimal()` parses at most 11 signed decimal bytes, or 12
  including one optional final NUL. It rejects empty/sign-only inputs, plus
  signs, whitespace, embedded NUL, trailing junk and signed 32-bit overflow.
  Errors leave output unchanged. This pure helper does not read device-tree
  properties; no board-specific `/chosen` parsing was added to the generic MFD.

The LK properties `atag,fg_swocv_v`, `atag,fg_swocv_i`, and `atag,shutdown_time`
survive the patched Linux FDT handoff as signed decimal bytes without a final
NUL. Therefore a future r1 consumer must use explicit property lengths.
The voltage/current are PTIM inputs, not the separately computed stock software
OCV. LK's PTIM timeout may use an ordinary ADC fallback without tagging its
source, and a producer early return can leave zero values. Shutdown-time units
and freshness were not established. Parsing success is not seed validation.

## Copy API and lifetime

`mt6357_boot_snapshot_get(parent, &copy)` returns a copy of immutable parent data.
The caller must hold a live parent device reference and have the appropriate
MFD module dependency. No pointer to parent devres escapes the API. It tries
the parent device lock, checks the exact bound driver and current MT6357 data,
and copies while still holding that lock. Driver-core teardown holds the same
lock through devres release. After unbind or binding a different driver, the
getter returns `-ENODEV` before dereferencing old driver data.

`-EAGAIN` means probe, removal or another reader currently owns the device lock.
In particular, a child probing synchronously during parent registration will see
this result. A future **probe** consumer must return `-EPROBE_DEFER` in that case;
returning `-EAGAIN` directly does not request normal driver-core deferred probing.
The snapshot is not silently discarded. Runtime callers may retry later.

Child ADC/gauge rebinds do not replace the parent's observation. Parent rebind
creates a new observation with a new interval and current register contents;
it does not prove that the PMIC itself has rebooted or captured new data. Copies
already returned remain owned by callers and must not be confused with the new
observation. There is no gauge consumer or new userspace ABI in this patch.

## Validation

Run the captured-vector suite with ordinary host Python:

```sh
source /rabbitr1/scripts/env.sh
PYTHONDONTWRITEBYTECODE=1 python3 /rabbitr1/src/mainline/rabbit-r1/scripts/test-mt6357-boot-snapshot.py
```

The suite needs the already pinned stock gauge C source from `sources.lock.json`
`ci_files`. It does not need extracted stock DTBs, firmware images, Unicorn or
the stock ELF. The independent 32,768 conversion outputs and 24 actual LK property
payloads are committed fixtures. Their hashes, stock instruction addresses and
firmware identities are recorded in `tests/battery/mt6357-boot-stock.json`.

To re-execute the conversion instructions, also provide the pinned stock ELF at
`/rabbitr1/out/stock-kernel.elf` and use the existing Unicorn environment:

```sh
source /rabbitr1/scripts/env.sh
PYTHONDONTWRITEBYTECODE=1 /rabbitr1/toolchains/boot-tools/bin/python /rabbitr1/src/mainline/rabbit-r1/scripts/test-mt6357-boot-snapshot.py --replay-stock
```

The complete production MFD probe, capture, getter, pure helpers and existing
ADC reset function run under ASan/UBSan with modeled framework/hardware I/O.
Tests cover all 65,536 capture words against the full stock conversion oracle,
all 65,536 metadata words, unavailable/unobserved/error publication, bounded LK
parsing and actual captured byte payloads. Full probe cases cover each failure
stage, unsupported models, and all 31 optional-read error combinations. A modeled
child-registration callback invokes the actual ADC reset with a destructive
register-bank model to verify ordering and preservation, including child reprobes.
Real pthread tests exercise 20,000 immutable copies while live input changes, an
entered getter held across removal, 100 late calls, foreign driver binding and
new observations on parent rebind.

Results, exact compiler command, source/fixture hashes and generated host C are
saved under `/rabbitr1/out/battery-review/boot-ocv/implementation/`. These tests
validate software ordering, boundaries and lifetime, not physical retention,
ADC calibration, boot readiness freshness, RTC coherence or actual SOC. A valid
seed still needs battery/profile identity, capture provenance and load/temperature
context, resistance/usable-capacity initialization, and a counter baseline with
reset/rollover invalidation. No guessed fallback is implemented.
