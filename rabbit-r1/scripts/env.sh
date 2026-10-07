#!/usr/bin/env bash
# Source this file; it never changes HOME or installs host packages.
set -euo pipefail
R1_ROOT=/rabbitr1
if [[ $(realpath "${BASH_SOURCE[0]%/*}/..") != "$R1_ROOT" ]]; then
    echo 'This workspace must stay in /rabbitr1.' >&2
    return 1 2>/dev/null || exit 1
fi
export TMPDIR="$R1_ROOT/.tmp"
export XDG_CACHE_HOME="$R1_ROOT/.cache"
export XDG_CONFIG_HOME="$R1_ROOT/.cache/config"
export XDG_DATA_HOME="$R1_ROOT/.cache/data"
export CCACHE_DIR="$R1_ROOT/.cache/ccache"
export PYTHONPYCACHEPREFIX="$R1_ROOT/.cache/pycache"
export PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_GLOBAL="$R1_ROOT/.gitconfig"
export GIT_CONFIG_NOSYSTEM=1
export GH_CONFIG_DIR="$R1_ROOT/.cache/gh"
export GIT_TERMINAL_PROMPT=0
export LC_ALL=C
mkdir -p "$TMPDIR" "$XDG_CACHE_HOME" "$R1_ROOT/logs" "$R1_ROOT/out" "$R1_ROOT/dist"
export KBUILD_BUILD_USER=r1-builder
export KBUILD_BUILD_HOST=r1-workspace
export KBUILD_BUILD_VERSION=1
export KCONFIG_NOTIMESTAMP=1
JOBS=${JOBS:-24}
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer.' >&2; exit 1; }

r1_build_time() {
    export SOURCE_DATE_EPOCH
    SOURCE_DATE_EPOCH=$(git -C "$1" show -s --format=%ct HEAD)
    export KBUILD_BUILD_TIMESTAMP
    KBUILD_BUILD_TIMESTAMP=$(date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %d %T UTC %Y')
}
