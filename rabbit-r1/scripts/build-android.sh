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

# Siso requires a relative config path; Soong test packaging rejects '..'.
# Keep the existing workspace cache behind Android's standard output path.
python3 - <<'PY'
from pathlib import Path
root = Path('/rabbitr1')
target = root / 'out/android'
link = root / 'src/android/out'
if target.resolve() != target:
    raise SystemExit('Android output target must not be redirected')
target.mkdir(parents=True, exist_ok=True)
if link.exists() or link.is_symlink():
    if not link.is_symlink() or link.resolve() != target:
        raise SystemExit('Preserving existing Android out path: ' + str(link))
else:
    link.symlink_to('../../out/android', target_is_directory=True)
PY
export OUT_DIR=out
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
