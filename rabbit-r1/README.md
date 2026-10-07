# rabbit r1 kernel workspace

Mainline-first development for the rabbit r1, confined to `/rabbitr1`.
The development tree is [itskenny0/android-kernel-mt6765-rabbitr1](https://github.com/itskenny0/android-kernel-mt6765-rabbitr1),
based on [fenolftalein-mamir/linux-mt6765](https://github.com/fenolftalein-mamir/linux-mt6765).
The pinned base is `cceb223d0c2d7ce52fade7bb6b9ef679c560bea7` on `mt6765-devel`.
Work is committed on `rabbit-r1/bringup`. GitHub authentication is required to
push this branch from the workspace.

**Status: kernel builds and offline checks, not a completed hardware port.**
The official 4.19.191 kernel and an experimental 7.1.0 mainline r1 kernel compile.
An experimental mtkclient boot package is built; there is no device boot test
or LineageOS ROM yet.
The Android target is **LineageOS 24.0**: the inspected official manifest selects
`android-17.0.0_r1`. Source-branch availability does not establish r1 compatibility.

## Results

| Output | Location | Meaning |
| --- | --- | --- |
| Mainline Image, Image.gz, r1 DTB, modules, config, vmlinux | `dist/mainline/` | Experimental bring-up build; storage and DVFS disabled initially |
| Official kernel Image, DTB/overlay, modules, config, vmlinux | `dist/vendor/` | Buildable vendor reference; not a bit-for-bit stock rebuild |
| Static AArch64 diagnostic initramfs | `dist/bringup/initramfs.cpio.gz` | RAM userspace with a development shell; not Android |
| mtkclient package | `dist/rabbit-r1-mainline-mtkclient.zip` | Boot/DTBO images, expdb logger and backup-based flash preparation |
| Source revisions and download checksums | `sources.lock.json` | Pins the inspected inputs |
| Mainline changes | `patches/mainline/` | Reapplicable patches against the new fork |
| Stock boot facts and decompiled DTs | `docs/research/` | Extracted from the supplied RabbitOS v0.8.293 archive |

Each kernel output directory contains `build.json` and `SHA256SUMS`.
The record includes the base revision, local diff hash, configuration hash,
compiler identity, and artifact/module hashes. Build logs are in `logs/`.
The initramfs contains an unauthenticated lab console, with optional USB ACM
enabled by `r1.usb=1`. It does not mount Android filesystems. The expdb boot
profile enables eMMC and writes kernel console logs to the first 18 MiB of the
20 MiB `expdb` partition, preserving its final 2 MiB from our writes.

Read [FLASHING.md](docs/FLASHING.md) for the two boot profiles, device backups,
slot selection, readback verification, log retrieval and restoration. The
package has passed offline format checks but remains untested on hardware.

[Validation results](docs/VALIDATION.md) distinguish passing build/regression
checks from the full DT schema failures and absent hardware tests.

## Build

Host tools used here: GNU make, GCC 13.3 AArch64 cross compiler and host compiler,
binutils, Python 3.12, flex, bison, bc, OpenSSL development headers, cpio and Git.
Packaging also uses `fdtput` from device-tree-compiler; target userspace checks
use `qemu-aarch64`.
The vendor build uses the downloaded Android Clang 11.0.1 (`clang-r383902`).
No system packages are installed by these scripts. Downloads, temporary files,
build outputs, Git user configuration and tool caches stay under `/rabbitr1`.
Installed host executables and their runtime libraries remain system tools.

```sh
cd /rabbitr1
source scripts/env.sh
python3 scripts/fetch-sources.py
bash scripts/build-mainline.sh
bash scripts/build-vendor.sh
bash scripts/build-initramfs.sh
python3 scripts/inspect-stock.py
python3 scripts/test-mt6357.py
python3 scripts/package-mtkclient.py
python3 scripts/test-expdb.py
python3 scripts/test-flash-package.py
```

`JOBS=16 bash scripts/build-mainline.sh` overrides the default 24 build jobs.
Fetching verifies archives and applies the patch series. It preserves a source
checkout descended from the lock instead of resetting it. An unrelated HEAD
stops fetching for inspection.

The kernel fork also builds independently using `rabbit_r1_defconfig`:

```sh
cd /rabbitr1
source scripts/env.sh
make -C src/mainline O=/rabbitr1/out/mainline ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- rabbit_r1_defconfig
make -C src/mainline O=/rabbitr1/out/mainline ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j24 Image.gz mediatek/mt6765-rabbit-r1.dtb modules
```

`configs/mainline-r1.config` records the overrides used to derive the standalone
defconfig from this fork's `dandelion_defconfig`. The standalone
`src/mainline/arch/arm64/configs/rabbit_r1_defconfig` is the build input.

## What changed

* Added a Rabbit-specific bring-up DT and defconfig using stock firmware/source
  evidence for UART, PMIC, USB and eMMC wiring. Unverified assumptions are marked.
* Corrected the MMC source clock provider in both MT6765 MMC nodes.
* Fixed a leftover merge marker and incompatible display-mutex table type in the new fork.
* Corrected MT6357 SRAM voltage-selector masks, implemented the vendor's
  revision-dependent SRAM mapping, and added hardware enable-status readback.
* Added a stock-image inspection workflow, deterministic initramfs packaging,
  build records, artifact checks and regulator regression checks.
* Added bounded expdb pstore logging, a raw ring decoder and mtkclient packaging
  with selected-slot backup and readback checks.

Read [the power investigation](docs/POWER.md) for exactly what the regulator
changes establish. Read [the porting plan](docs/PORTING.md) for the bootloader,
hardware and LineageOS work still required. Compilation and mocked register tests
cannot establish charging behavior, bootability or electrical correctness.
