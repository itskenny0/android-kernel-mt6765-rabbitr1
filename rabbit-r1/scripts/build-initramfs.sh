#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$(realpath "$0")")/env.sh"
cd "$R1_ROOT"
r1_build_time "$R1_ROOT/src/mainline"
mkdir -p out/busybox dist/bringup
exec > >(tee logs/initramfs-build.log) 2>&1
install -m644 configs/busybox.config out/busybox/.config
make -C src/busybox-1.37.0 O="$R1_ROOT/out/busybox" CROSS_COMPILE=aarch64-linux-gnu- oldconfig
make -C src/busybox-1.37.0 O="$R1_ROOT/out/busybox" CROSS_COMPILE=aarch64-linux-gnu- -j"$JOBS"
aarch64-linux-gnu-gcc -static -Os -Wall -Wextra -Werror -o out/busybox/expdb-map initramfs/expdb-map.c
aarch64-linux-gnu-gcc -static -Os -Wall -Wextra -Werror -o out/busybox/expdb-checkpoint initramfs/expdb-checkpoint.c
python3 scripts/make-initramfs.py
