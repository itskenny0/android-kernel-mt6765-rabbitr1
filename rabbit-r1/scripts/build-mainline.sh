#!/usr/bin/env bash
set -euo pipefail
r1_experimental_soc=0
if (($#)); then
    if [[ $# -ne 1 || $1 != --experimental-soc ]]; then
        echo "Usage: $0 [--experimental-soc]" >&2
        exit 2
    fi
    r1_experimental_soc=1
fi
source "$(dirname "$(realpath "$0")")/env.sh"
cd "$R1_ROOT"
src="$R1_ROOT/src/mainline"
out="$R1_ROOT/out/mainline"
dist="$R1_ROOT/dist/mainline"
r1_build_time "$src"
mkdir -p "$out" "$dist"
exec > >(tee "$R1_ROOT/logs/mainline-build.log") 2>&1
kmake=(make -C "$src" O="$out" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-)
"${kmake[@]}" rabbit_r1_defconfig
r1_soc_expected=n
if ((r1_experimental_soc)); then
    "$src/scripts/config" --file "$out/.config" --enable BATTERY_MT6357_R1_SOC
    "${kmake[@]}" olddefconfig
    r1_soc_expected=y
fi
r1_soc_actual=$("$src/scripts/config" --file "$out/.config" --state BATTERY_MT6357_R1_SOC)
if [[ $r1_soc_actual != "$r1_soc_expected" ]]; then
    echo "Requested CONFIG_BATTERY_MT6357_R1_SOC=$r1_soc_expected, got $r1_soc_actual" >&2
    exit 1
fi
echo "CONFIG_BATTERY_MT6357_R1_SOC=$r1_soc_actual"
"${kmake[@]}" -j"$JOBS" Image.gz mediatek/mt6765-rabbit-r1.dtb modules
"${kmake[@]}" savedefconfig
install -m644 "$out/arch/arm64/boot/Image.gz" "$out/arch/arm64/boot/Image" "$dist/"
install -m644 "$out/arch/arm64/boot/dts/mediatek/mt6765-rabbit-r1.dtb" "$dist/"
install -m644 "$out/.config" "$dist/config"
install -m644 "$out/defconfig" "$dist/defconfig"
install -m644 "$out/System.map" "$out/Module.symvers" "$out/vmlinux" "$dist/"
python3 "$R1_ROOT/scripts/validate-kernel.py" mainline
python3 "$R1_ROOT/scripts/record-build.py" mainline
