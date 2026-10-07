# Offline validation — 2026-10-07

| Check | Result | Evidence |
| --- | --- | --- |
| Mainline Image.gz, r1 DTB and modules | PASS, Linux 7.1.0-rabbit-r1-bringup+ | `logs/mainline-build.log`, `dist/mainline/build.json` |
| Official r1 Image.gz, DTB/overlay and modules | PASS, Linux 4.19.191-g8167c8c1087f | `logs/vendor-build.log`, `dist/vendor/build.json` |
| AArch64 Image/ELF, gzip consistency and selected config | PASS | `logs/kernel-validation.log` |
| r1 identity, loader memory path, MMC clock provider/IDs, storage gate, USB role and I2C dividers in DTB | PASS | `scripts/validate-kernel.py` |
| Original MMC source-clock regression | Rejected as expected | `logs/mmc-regression-before.log` |
| MT6357 register mappings and new helpers | PASS | `logs/mt6357-tests.log` |
| Original SRAM selector-mask regression | Rejected as expected | `logs/mt6357-selector-regression-before.log` |
| I2C programmable-divider calculation | PASS: 70 MT8183 and 140 MT6765 bank/setup cases, invalid inputs and count boundary | `logs/i2c-tests.log` |
| MT6765 timing vs shipped kernel | PASS: 180 identical register sets, 20 rejected rate combinations and count-boundary check | `logs/i2c-stock-timing.log`, `out/i2c-stock-timing.json`; selected AArch64 setup fragment only |
| MT6765 transfer/IRQ handling | PASS: AP gate, faults, DMA cleanup, native START and allocation errors; MT8183 restart regression checks | `logs/i2c-irq-tests.log`; MMIO/DMA model only |
| MT6765 FIFO transfers | PASS: 108 FIFO cases, failed reads unchanged, mixed 8/9-byte DMA pairs, mode transitions and real core quirks | `logs/i2c-irq-tests.log`; modeled MMIO only |
| Shipped kernel FIFO setup | PASS: 48 exact write sequences and 22 DMA selections on both banks | `logs/i2c-stock-fifo.log`, `out/i2c-stock-fifo.json`; selected setup instructions, no RX or bus activity |
| FIFO limit, auxiliary length, DMA flags, failed read, bank and START regressions | All rejected by runtime assertions | `logs/i2c-fifo-regression-*.log` |
| Stock ATF I2C service | PASS: 64 selected-dispatch cases, firmware/DT IDs, exact writes, rejected requests and signed return | `logs/i2c-firmware-tests.log`, `out/i2c-firmware-audit.json`; excludes EL3 entry/caller checks and hardware |
| MT6765 AP channel | PASS: 36 transfer cases, adapter entry, secure IDs, firmware/clock failures, recovery and suspend/resume | `logs/i2c-irq-tests.log`; modeled hardware |
| Channel bank, firmware refusal, shared reset and FIFO regressions | All rejected | `logs/i2c-channel-regression-*.log` |
| Native I2C divider, timeout, raw-count and clock-refresh regressions | All rejected by transfer assertions | `logs/i2c-native-regression-*.log` |
| I2C gate, terminal IRQ, DMA cleanup and failed-copy regressions | All rejected | `logs/i2c-irq-regression-*.log` |
| MT6370 ADC selection and MIVR workaround | PASS: compact/full ADC lists, current units and error cleanup | `logs/mt6370-tests.log` |
| MT6370 positional-index and current-scale regressions | Both rejected | `logs/mt6370-regression-*.log` |
| MT6370 backlight configuration and brightness | PASS: 65,536 property/register cases, both brightness formats, revisions and errors | `logs/mt6370-backlight-tests.log`; production callbacks with stubs and ASan/UBSan |
| Shipped backlight setup | PASS: 48 instruction fixtures, four synthetic revision cases and stock-DT values | `logs/stock-backlight-tests.log`, `out/stock-backlight-audit.json`; PMIC transport modeled |
| Backlight masks, mode bit, shutdown polarity, revision and brightness regressions | All seven rejected at runtime | `logs/mt6370-backlight-regression-*.log` |
| Compiled r1 power-device wiring | PASS against stock FDT, including IRQs, pin muxes and child gates | `logs/r1-power-tests.log` |
| MT6370 and hwmon schemas | PASS, zero diagnostics for the new power nodes | `logs/r1-power-schema.log`, `out/r1-power-schema.json` |
| CST836 report/transport/PM logic | PASS with I2C/input/GPIO stubs and ASan/UBSan | `logs/cst836-tests.log`; no hardware events |
| CST836 driver and draft r1 touch wiring | AArch64 object compiled; DT matches stock pins/address/dimensions | `logs/cst836-compile.log`, `logs/r1-touch-tests.log`; I2C4 enabled for experimental probing |
| CST836 binding/example and board node schema | PASS, zero CST836 diagnostics | `logs/cst836-binding.log`, `logs/cst836-schema.log`, `out/cst836-schema.json` |
| CST836 count, short-read and failed-resume regressions | All rejected by production-code tests | `logs/cst836-regression-*.log` |
| Original I2C fixed-divider regression | Rejected: 100 kHz request decodes to 500 kHz | `logs/i2c-regression-before.log` |
| Patch series on pristine affected files | PASS, all 18 files match working tree | `logs/patch-check.log` |
| Fetch workflow with existing sources/patches | PASS, checksums verified and patches detected | `logs/fetch-check.log` |
| Stock image extraction/overlay merge | PASS | `logs/inspect-stock.log`, `docs/research/stock-facts.json` |
| Initramfs archive inspection, target-shell syntax and static AArch64 BusyBox smoke test | PASS, userspace only | `logs/initramfs-contents.log`, `out/busybox/busybox` |
| Initramfs repeated packaging | Identical SHA256 | `logs/initramfs-reproducibility.log` |
| Bounded expdb mapping and pstore ring decoder | PASS, host tests only | `logs/expdb-tests.log` |
| RAM and expdb boot images | PASS: v2 headers, IDs, tables, addresses, padding and AOSP unpack | `logs/flash-package-tests.log` |
| Flash preparation and restore | PASS: synthetic GPT/backups, CRC rejection, AVB flags, preview scripts | `logs/flash-package-tests.log` |
| Matching pstore modules and target shell syntax | PASS | `logs/flash-package-tests.log` |
| LK warning patches and instruction verification | PASS, 102 warning bytes changed | `logs/build-lk.log` |
| LK relock guard | PASS: real Fastboot registration, FAIL response, no memory writes; unknown/tampered images rejected | `logs/lk-tests.log` |
| Linux DT handoff and memory ownership | PASS, actual Thumb instructions with modeled external calls | `logs/lk-tests.log` |
| Stock LK board overlay and LineageOS splash | PASS, GPIO/charger tree retained; 58 logo slots unchanged | `logs/lk-tests.log` |
| mtkclient package repeated packaging | Identical SHA256 | `logs/package-reproducibility.log` |
| Output SHA256SUMS and recorded source-diff hashes | PASS | `dist/{mainline,vendor,bringup}/` |
| r1 board-compatible schema | PASS | `logs/r1-board-schema.log` (empty diagnostics) |
| MT6765 I2C schema | PASS, all seven controller nodes | `logs/mt6765-i2c-schema.log`, `out/mt6765-i2c-schema.json` |
| Full r1 DT schema validation | **FAIL: 41 diagnostics remain** | `logs/r1-dt-validate.log`, `out/r1-i2c-full-schema.json` |
| Kernel checkpatch on initial five patches | No errors; style/submission warnings | `logs/checkpatch.log` |
| I2C divider patch checkpatch | No errors or warnings | `logs/i2c-checkpatch.log` |
| I2C native timing/binding patches checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/i2c-native-checkpatch.log` |
| I2C FIFO patch checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/i2c-fifo-checkpatch.log` |
| I2C channel patches checkpatch | No errors or warnings | `logs/i2c-channel-checkpatch.log` |
| MT6370 current-measurement patch checkpatch | No errors or warnings | `logs/mt6370-checkpatch.log` |
| MT6370 backlight patch checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/mt6370-backlight-checkpatch.log` |
| Actual r1 boot, charging, display, storage or LineageOS | **NOT TESTED** | No device available |

The full schema failures are recorded, not inferred from the validator's exit
code (the tool can return success while printing failures). Most concern the
fork's incomplete MT6765 bindings. The r1 `/memory` name is an explicit
stock-loader compatibility exception, and its conservative MMC pin-state draft
also needs binding work. See [PORTING.md](PORTING.md). Schema conformance is not
hardware verification.

The official vendor build retains warnings from the published sources,
including duplicate accdet exports. It is a reference build, not a verified
replacement for the different kernel commit inside the supplied stock image.

The host regulator harness uses production tables and new helper bodies with
mocked register reads. QEMU testing here executes only the static BusyBox
userspace binary; it does not emulate an MT6765 board or boot either kernel.
The expdb tests exercise the actual DM table builder, partition-name filter
and raw ring decoder. They do not execute device-mapper ioctls against a real
block device. Unicorn executes the LK handoff routine with modeled allocation,
accessor and copy calls; it does not emulate the complete bootloader or board.
No flash, restore, persistence or panic-recovery result is claimed.

To repeat the main checks:

```sh
cd /rabbitr1
source scripts/env.sh
python3 scripts/validate-kernel.py mainline
python3 scripts/validate-kernel.py vendor
python3 scripts/test-mt6357.py
python3 scripts/test-mt6370.py
python3 scripts/test-mt6370-backlight.py
toolchains/boot-tools/bin/python scripts/test-stock-backlight.py
python3 scripts/test-r1-power.py
python3 scripts/test-i2c.py
python3 scripts/test-i2c-irq.py
toolchains/boot-tools/bin/python scripts/test-i2c-stock-timing.py
toolchains/boot-tools/bin/python scripts/test-i2c-stock-fifo.py
python3 scripts/test-cst836.py
python3 scripts/test-r1-touch.py
python3 scripts/check-patches.py
python3 scripts/test-expdb.py
toolchains/boot-tools/bin/python scripts/test-lk.py
toolchains/boot-tools/bin/python scripts/test-i2c-firmware.py
python3 scripts/test-flash-package.py
```

The schema tools live in `toolchains/dt-schema`, installed only within this
workspace. `configs/dtschema-requirements.txt` records their package versions.
The checked schema cache is under `out/dt-schema`. To regenerate and inspect:

```sh
cd /rabbitr1
source scripts/env.sh
export PATH=/rabbitr1/toolchains/dt-schema/bin:$PATH
make -C src/mainline O=/rabbitr1/out/dt-schema ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- rabbit_r1_defconfig
make -C src/mainline O=/rabbitr1/out/dt-schema ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j8 dtbs_check DT_SCHEMA_FILES=arm/mediatek.yaml
dt-validate -s out/dt-schema/Documentation/devicetree/bindings/processed-schema.json dist/mainline/mt6765-rabbit-r1.dtb
```

Neither source reprovisioning on a different host nor a second completely clean
kernel build was used to claim cross-host bit reproducibility. Source/config/tool
identity and artifact hashes are recorded so that can be checked later.
