# rabbit r1 mainline kernel

Experimental rabbit r1 support on the fenolftalein-mamir MT6765 kernel fork.
The r1 target builds, and an mtkclient boot package can be generated. There is no
hardware boot test or LineageOS image yet.

The package retains the stock DTBO for LK board setup and includes a patched LK
for the mainline DT handoff, warning removal and a separate LineageOS splash.
See [LK.md](rabbit-r1/docs/LK.md) for the patches and offline tests.

The changes add the r1 device tree and defconfig, fix MMC source clocks and
MT6357 SRAM regulator handling, and enable pstore modules for logs in `expdb`.
The expdb boot profile writes the first 18 MiB of that partition; its final
2 MiB are excluded. A separate RAM profile leaves eMMC disabled.

All host work belongs under `/rabbitr1`. With this checkout at
`/rabbitr1/src/mainline`, install the tracked tooling and build:

```sh
cd /rabbitr1
python3 src/mainline/rabbit-r1/scripts/install-workspace.py
source scripts/env.sh
python3 scripts/fetch-sources.py
bash scripts/build-mainline.sh
bash scripts/build-initramfs.sh
python3 scripts/inspect-stock.py
python3 -m venv toolchains/boot-tools
toolchains/boot-tools/bin/pip install -r configs/boot-tools-requirements.txt
toolchains/boot-tools/bin/python scripts/build-lk.py
toolchains/boot-tools/bin/python scripts/test-lk.py
python3 scripts/package-mtkclient.py
python3 scripts/test-expdb.py
python3 scripts/test-flash-package.py
```

The source fetch verifies pinned archives, including RabbitOS v0.8.293 and the
mtkclient source release. Host packages are not installed by these scripts.
Dependencies and all build steps are in the [workspace guide](rabbit-r1/README.md).

**Do not relock until the complete stock firmware package, including every LK
slot, has been restored.** Patched LK can fail signature verification and prevent
booting. The build blocks its Fastboot lock handler; other loaders and direct
`seccfg` writes remain outside that protection.

Read the [flashing guide](rabbit-r1/docs/FLASHING.md) before using the package.
It covers full device backups, explicit slot selection, readback verification,
log recovery and restore. Persistent logs require working eMMC; early crashes
and hard hangs can still need UART. Nothing here establishes correct charging,
display, radio or suspend behavior.

See [validation](rabbit-r1/docs/VALIDATION.md),
[power notes](rabbit-r1/docs/POWER.md) and the
[LineageOS 24 porting plan](rabbit-r1/docs/PORTING.md).
The original Linux build documentation remains in [README](README).

GitHub Actions builds the kernel and experimental mtkclient package on pushes
and pull requests. See [CI.md](rabbit-r1/docs/CI.md) for checks and artifacts.
