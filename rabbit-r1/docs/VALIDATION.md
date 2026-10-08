# Offline validation — 2026-10-08

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
| Panel init/suspend vs shipped kernel | PASS: 39 command payloads, delays, reset waveform, mode fields and DT wiring | `logs/r1-panel-tests.log`, `out/r1-panel-audit.json`; selected stock instructions and modeled transport |
| Panel transport errors and enable gate | PASS: 82 failed/short commands, GPIO failure, reset cleanup, prepare/unprepare cycle and prepared-state gate | `scripts/test-r1-panel.py`; production callbacks with ASan/UBSan |
| Panel payload, packet type, reset, short-write and sleep-error regressions | All five rejected | `logs/r1-panel-regression-*.log` |
| MediaTek DSI transfer return | PASS: seven packet types, lengths 0..64, four modes, errors and one-byte read regression | `logs/dsi-transfer-tests.log`; host callback with modeled MMIO/IRQ/transport |
| Zero-byte DSI write regression | Rejected | `logs/dsi-transfer-regression.log` |
| Native MT6765 DSI timing vs shipped kernel | PASS: 12 D-PHY rates, 24 video/pixel combinations, eight lane/clock combinations and fractional r1 mode | `logs/dsi-timing-tests.log`, `out/dsi-timing-audit.json`; selected setup instructions with modeled MMIO |
| DSI mode validation and power errors | PASS: invalid fields/rates/formats/lanes, rate overflow, clock/PHY failure cleanup and unchanged MT8183 setup | `scripts/test-dsi-timing.py`; production callbacks with ASan/UBSan |
| DSI timing, alignment, pixel format, clock-LP, field bound, overflow, PHY error and native-selection regressions | All eight rejected at runtime | `logs/dsi-timing-regression-*.log` |
| Native MT6765 DSI host, DRM driver, r1 panel and DTB | AArch64 objects and DTB compiled | `logs/mt6765-dsi-build.log` |
| Native MT6765 PHY vs shipped kernel | PASS: 57 exact setup/shutdown sequences, four repeated calibration fixtures and fractional PCW | `logs/mt6765-phy-tests.log`, `out/mt6765-phy-audit.json`; MMIO, clocks and delays modeled |
| PHY error cleanup and shared power callback order | PASS: bad rates/reference clocks, failed clocks, atomic context and legacy ordering | `scripts/test-mt6765-phy.py`; production callbacks with ASan/UBSan |
| PHY delays, analog clock, divider boundary, calibration, SSC, firmware handoff, clock cleanup and atomic-context regressions | All eight rejected at runtime | `logs/mt6765-phy-regression-*.log` |
| Native PHY and shared driver | AArch64 objects compiled | `logs/mt6765-phy-build.log` |
| MT6765 MMSYS routing and mutex vs shipped instructions | PASS: four initial states, connect/disconnect/reconnect, stock display mutexes and all ten mainline handles | `logs/mt6765-display-path-tests.log`, `out/mt6765-display-path-audit.json`; chosen path fixture and modeled MMIO |
| Shared mutex and compiled RDMA clock | PASS: MT8183 DSI/OVL/RDMA, MT2712 MOD1, stock clock provider/ID/gate | `scripts/test-mt6765-display-path.py`; production callbacks with ASan/UBSan and compiled DT |
| Missing routes, narrow masks, DSI module enable/removal, legacy MOD1 and RDMA clock regressions | All nine rejected at runtime | `logs/mt6765-display-path-regression-*.log` |
| MMSYS, mutex and RDMA DT fixes | AArch64 objects and DTB compiled | `logs/mt6765-display-path-build.log` |
| Native RDMA FIFO/QoS vs shipped instructions | PASS: 108 traces, direct/memory video input, vendor clock states and inherited registers; equivalent-pixel-rate fixtures for other refresh rates | `logs/mt6765-rdma-tests.log`, `out/mt6765-rdma-audit.json` |
| RDMA reset, mode and clock handling | PASS: modeled reset transitions/timeouts, clock errors, IRQ-safe configuration, geometry/bandwidth bounds, queued writes, input transitions and MT8183 regression | `scripts/test-mt6765-rdma.py`; production callbacks with ASan/UBSan |
| RDMA FIFO, mode, request flags, SRAM, clocks, reset, startup, width, pending depth and refresh-rate regressions | All twelve rejected at runtime | `logs/mt6765-rdma-regression-*.log` |
| Native RDMA binding/example and compiled node | PASS: zero RDMA diagnostics; FIFO override rejected | `logs/mt6765-rdma-binding.log`, `logs/mt6765-rdma-schema.log`, `out/mt6765-rdma-schema{,-regression}.json`; unrelated tphy warning and unavailable optional yamllint noted |
| Native RDMA, DRM lookup and DTB | AArch64 objects and DTB compiled | `logs/mt6765-rdma-build.log` |
| Native PHY binding/example and compiled node | PASS, zero PHY-node diagnostics | `logs/mt6765-phy-binding.log`, `logs/mt6765-phy-schema.log`, `out/mt6765-phy-schema.json`; unrelated tphy warning and optional yamllint absence noted |
| Native DSI binding/example and compiled host node | PASS, zero host diagnostics | `logs/mt6765-dsi-binding.log`, `logs/mt6765-dsi-schema.log`, `out/mt6765-dsi-schema.json`; inherited PHY warning and unavailable optional yamllint noted |
| Panel driver and DSI host | AArch64 objects compiled | `logs/r1-panel-build.log`, `logs/dsi-transfer-build.log` |
| Panel binding/example and compiled node | PASS, zero panel diagnostics | `logs/r1-panel-binding.log`, `logs/r1-panel-schema.log`, `out/r1-panel-schema.json`; unrelated inherited PHY warning and unavailable optional yamllint noted |
| Compiled r1 power-device wiring | PASS against stock FDT, including IRQs, pin muxes and child gates | `logs/r1-power-tests.log` |
| MT6370 and hwmon schemas | PASS, zero diagnostics for the new power nodes | `logs/r1-power-schema.log`, `out/r1-power-schema.json` |
| CST836 report/transport/PM logic | PASS with I2C/input/GPIO stubs and ASan/UBSan | `logs/cst836-tests.log`; no hardware events |
| CST836 driver and draft r1 touch wiring | AArch64 object compiled; DT matches stock pins/address/dimensions | `logs/cst836-compile.log`, `logs/r1-touch-tests.log`; I2C4 enabled for experimental probing |
| CST836 binding/example and board node schema | PASS, zero CST836 diagnostics | `logs/cst836-binding.log`, `logs/cst836-schema.log`, `out/cst836-schema.json` |
| CST836 count, short-read and failed-resume regressions | All rejected by production-code tests | `logs/cst836-regression-*.log` |
| Original I2C fixed-divider regression | Rejected: 100 kHz request decodes to 500 kHz | `logs/i2c-regression-before.log` |
| Patch series on pristine affected files | PASS, all 33 files match working tree | `logs/patch-check.log` |
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
| Full r1 DT schema validation | **FAIL: 36 diagnostics remain** | `logs/r1-dt-validate.log`, `out/mt6765-rdma-full-schema.json` |
| Kernel checkpatch on initial five patches | No errors; style/submission warnings | `logs/checkpatch.log` |
| I2C divider patch checkpatch | No errors or warnings | `logs/i2c-checkpatch.log` |
| I2C native timing/binding patches checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/i2c-native-checkpatch.log` |
| I2C FIFO patch checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/i2c-fifo-checkpatch.log` |
| I2C channel patches checkpatch | No errors or warnings | `logs/i2c-channel-checkpatch.log` |
| MT6370 current-measurement patch checkpatch | No errors or warnings | `logs/mt6370-checkpatch.log` |
| MT6370 backlight patch checkpatch | No code/style errors or warnings (`--no-signoff`) | `logs/mt6370-backlight-checkpatch.log` |
| Panel patches checkpatch | No errors; two submission warnings (MAINTAINERS and combined binding/code patch) | `logs/r1-panel-checkpatch.log` |
| Native DSI binding/timing patches checkpatch | No errors or warnings (`--no-signoff`) | `logs/dsi-timing-checkpatch.log` |
| Native PHY binding/backend patches checkpatch | No errors; one generic new-file MAINTAINERS warning (existing wildcard covers it) | `logs/mt6765-phy-checkpatch.log` |
| MT6765 display-path patch checkpatch | No errors or warnings (`--no-signoff`) | `logs/mt6765-display-path-checkpatch.log` |
| Native RDMA patches checkpatch | No errors or warnings (`--no-signoff`) | `logs/mt6765-rdma-checkpatch.log` |
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
python3 scripts/test-dsi-transfer.py
toolchains/boot-tools/bin/python scripts/test-r1-panel.py
toolchains/boot-tools/bin/python scripts/test-dsi-timing.py
toolchains/boot-tools/bin/python scripts/test-mt6765-phy.py
toolchains/boot-tools/bin/python scripts/test-mt6765-display-path.py
toolchains/boot-tools/bin/python scripts/test-mt6765-rdma.py
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
