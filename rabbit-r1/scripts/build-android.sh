#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the initial Android integration targets, or explicit targets supplied.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/env.sh"

R1_ANDROID_JOBS=${R1_ANDROID_JOBS:-8}
[[ $R1_ANDROID_JOBS =~ ^[1-9][0-9]*$ ]] || {
    echo 'R1_ANDROID_JOBS must be a positive integer.' >&2
    exit 1
}
export REPO_CONFIG_DIR="$R1_ROOT/.cache/repo" REPO_NO_INTERACTIVE=1
export GOCACHE="$R1_ROOT/.cache/go-build" GOPATH="$R1_ROOT/.cache/go"
export GRADLE_USER_HOME="$R1_ROOT/.cache/gradle" ANDROID_USER_HOME="$R1_ROOT/.cache/android"
cd "$R1_ROOT/src/android"

# Siso expects its generated config path relative to the Android source root.
export OUT_DIR=../../out/android
python3 "$R1_ROOT/scripts/apply-android-patches.py" --apply
python3 "$R1_ROOT/scripts/install-lineage-device.py"
python3 "$R1_ROOT/toolchains/git-repo/repo" manifest -r -o "$R1_ROOT/out/lineage-resolved.xml"

targets=("$@")
if ((${#targets[@]} == 0)); then
    targets=(R1ChargingSettings r1-charging r1-expdb
             android.hardware.boot-service.r1 android.hardware.boot-service.r1_recovery
             selinux_policy)
fi

# Android's shell setup expects unset variables and manages command failures.
set +eu
source build/envsetup.sh
lunch lineage_r1 trunk_staging userdebug > "$R1_ROOT/logs/android-lunch.log" 2>&1
r1_status=$?
if ((r1_status)); then
    cat "$R1_ROOT/logs/android-lunch.log"
    exit "$r1_status"
fi
echo "Android build log: $R1_ROOT/logs/android-targeted-build.log"
m -j"$R1_ANDROID_JOBS" "${targets[@]}" > "$R1_ROOT/logs/android-targeted-build.log" 2>&1
r1_status=$?
tail -70 "$R1_ROOT/logs/android-targeted-build.log"
exit "$r1_status"
