# Offline validation — 2026-10-07

| Check | Result | Evidence |
| --- | --- | --- |
| Mainline Image.gz, r1 DTB and modules | PASS, Linux 7.1.0-rabbit-r1-bringup+ | `logs/mainline-build.log`, `dist/mainline/build.json` |
| Official r1 Image.gz, DTB/overlay and modules | PASS, Linux 4.19.191-g8167c8c1087f | `logs/vendor-build.log`, `dist/vendor/build.json` |
| AArch64 Image/ELF, gzip consistency and selected config | PASS | `logs/kernel-validation.log` |
| r1 identity, loader memory path, MMC clock provider/IDs, storage gate and USB role in DTB | PASS | `scripts/validate-kernel.py` |
| Original MMC source-clock regression | Rejected as expected | `logs/mmc-regression-before.log` |
| MT6357 register mappings and new helpers | PASS | `logs/mt6357-tests.log` |
| Original SRAM selector-mask regression | Rejected as expected | `logs/mt6357-selector-regression-before.log` |
| Patch series on pristine affected files | PASS, all 8 files match working tree | `logs/patch-check.log` |
| Fetch workflow with existing sources/patches | PASS, checksums verified and patches detected | `logs/fetch-check.log` |
| Stock image extraction/overlay merge | PASS | `logs/inspect-stock.log`, `docs/research/stock-facts.json` |
| Initramfs archive inspection, target-shell syntax and static AArch64 BusyBox smoke test | PASS, userspace only | `logs/initramfs-contents.log`, `out/busybox/busybox` |
| Initramfs repeated packaging | Identical SHA256 | `logs/initramfs-reproducibility.log` |
| Bounded expdb mapping and pstore ring decoder | PASS, host tests only | `logs/expdb-tests.log` |
| RAM and expdb boot images | PASS: v2 headers, IDs, tables, addresses, padding and AOSP unpack | `logs/flash-package-tests.log` |
| Flash preparation and restore | PASS: synthetic GPT/backups, CRC rejection, AVB flags, preview scripts | `logs/flash-package-tests.log` |
| Matching pstore modules and target shell syntax | PASS | `logs/flash-package-tests.log` |
| mtkclient package repeated packaging | Identical SHA256 | `logs/package-reproducibility.log` |
| Output SHA256SUMS and recorded source-diff hashes | PASS | `dist/{mainline,vendor,bringup}/` |
| r1 board-compatible schema | PASS | `logs/r1-board-schema.log` (empty diagnostics) |
| Full r1 DT schema validation | **FAIL: 48 diagnostics remain** | `logs/r1-dt-validate.log` |
| Kernel checkpatch on five patches | No errors; style/submission warnings | `logs/checkpatch.log` |
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
block device. No flash, restore, persistence or panic-recovery result is claimed.

To repeat the main checks:

```sh
cd /rabbitr1
source scripts/env.sh
python3 scripts/validate-kernel.py mainline
python3 scripts/validate-kernel.py vendor
python3 scripts/test-mt6357.py
python3 scripts/check-patches.py
python3 scripts/test-expdb.py
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
