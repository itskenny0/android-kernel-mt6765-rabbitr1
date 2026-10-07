#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$(realpath "$0")")/env.sh"
cd "$R1_ROOT"
src="$R1_ROOT/src/kernel"
out="$R1_ROOT/out/vendor"
dist="$R1_ROOT/dist/vendor"
export PATH="$R1_ROOT/toolchains/clang-r383902/bin:$PATH"
[[ -x "$R1_ROOT/toolchains/clang-r383902/bin/clang" ]] || { echo 'Run scripts/fetch-sources.py first.' >&2; exit 1; }
r1_build_time "$src"
mkdir -p "$out" "$dist"
exec > >(tee "$R1_ROOT/logs/vendor-build.log") 2>&1
kmake=(make -C "$src" O="$out" ARCH=arm64 LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu-)
"${kmake[@]}" r1_defconfig
"${kmake[@]}" -j"$JOBS" Image.gz modules mediatek/mt6765.dtb mediatek/k65v1_64_bsp.dtb
install -m644 "$out/arch/arm64/boot/Image.gz" "$out/arch/arm64/boot/Image" "$dist/"
install -m644 "$out/arch/arm64/boot/dts/mediatek/mt6765.dtb" "$out/arch/arm64/boot/dts/mediatek/k65v1_64_bsp.dtb" "$dist/"
install -m644 "$out/.config" "$dist/config"
install -m644 "$out/System.map" "$out/Module.symvers" "$out/vmlinux" "$dist/"
python3 "$R1_ROOT/scripts/validate-kernel.py" vendor
python3 "$R1_ROOT/scripts/record-build.py" vendor
