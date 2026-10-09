# Android pmsg regression checks

Run from `/rabbitr1` after installing the workspace and fetching CI inputs:

```sh
source scripts/env.sh
python3 scripts/fetch-sources.py --profile ci
python3 scripts/test-android-pmsg.py
```

The existing CI fetch step supplies the inputs; the test runner does not fetch
anything or require `src/android`. It needs Clang, Git, Python and a C++20 host
standard library. Results and compiler logs go to `out/android-pmsg`.

The runner compiles the actual pmsg reader and writer from
[`system/logging` at `336e1707c8258adf1f1c5c5511218b533f3a22e8`](https://github.com/LineageOS/android_system_logging/tree/336e1707c8258adf1f1c5c5511218b533f3a22e8/liblog),
using the minimal header closure from that revision and
[`cutils/list.h` at `7c3f32fe89c52b4176fd288ba5bb4f907809c202`](https://github.com/LineageOS/android_system_core/blob/7c3f32fe89c52b4176fd288ba5bb4f907809c202/libcutils/include/cutils/list.h).
Every input has a commit URL, byte count and SHA-256 in `sources.lock.json` under
`downloads/pmsg-ci/`. The canonical Android patch is applied only to temporary
copies, with original and resulting hashes checked against `series.json`.

`pmsg.cpp` replaces only the six I/O and clock functions named by the linker
wrappers. These wrappers enforce expected paths and flags and use in-memory
records; they never forward calls to host devices. Production parsing, log
structures, filtering, descriptor state and retry logic are compiled unchanged.

The 33 scenarios run with both userdebug and user build policy, under ASan and
UBSan. They cover backend selection, record contents, missing and invalid data,
permissions errors, descriptor zero, late device creation, retry limits, cached
errors, and concurrent writes. Separate negative controls retain each original
source file and must fail at its corresponding regression. No Android source
checkout or kernel files are modified.

These checks do not establish target ABI compatibility, SELinux access, reboot
persistence or panic logging. Those still require the Android build and a device.
