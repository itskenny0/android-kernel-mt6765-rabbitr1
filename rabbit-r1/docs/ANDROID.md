# LineageOS 24 build integration

The initial r1 product is tracked under `android/device`; the charging UI and
service remain under `android/charging`. The native build and full policy build
are not verified yet. No Android image has been produced or added to the
mtkclient package.

`android/local_manifests/r1.xml` pins the common mainline device tree and its ten
direct and transitive dependencies. Use it with the LineageOS manifest commit in `sources.lock.json`.
The upstream manifest uses the Android 17 release tag for AOSP projects and
moving Lineage branches for other projects; record a resolved manifest after
sync and account for any local source edits when recording build provenance.

The current workspace has an initialized checkout at `/rabbitr1/src/android`.
The repo launcher is `/rabbitr1/toolchains/git-repo/repo`. Keep its configuration
in `/rabbitr1/.cache/repo` and its user Git configuration in the workspace's XDG
configuration directory. Do not move caches or build output outside `/rabbitr1`.

After building and checking the diagnostic kernel and mtkclient package:

```bash
source /rabbitr1/scripts/env.sh
export REPO_CONFIG_DIR=/rabbitr1/.cache/repo REPO_NO_INTERACTIVE=1
mkdir -p /rabbitr1/src/android/.repo/local_manifests
cp /rabbitr1/android/local_manifests/r1.xml /rabbitr1/src/android/.repo/local_manifests/r1.xml
cd /rabbitr1/src/android
python3 /rabbitr1/toolchains/git-repo/repo sync -c -j6 --no-tags --no-clone-bundle
python3 /rabbitr1/toolchains/git-repo/repo manifest -r -o /rabbitr1/out/lineage-resolved.xml
python3 /rabbitr1/scripts/install-lineage-device.py
```

The installer stages verified build inputs, including the stock overlay that LK
still uses for its private vendor tree. It changes only the mainline DTB's eMMC
status; the other device gates are retained. It does not flash a device or place
patched LK into an Android OTA.

Once sync completes, run the actual product configuration and targeted build:

```bash
bash /rabbitr1/scripts/build-android.sh
```

The helper checks and applies the pinned Android compatibility patches, stages
the device files and kernel, records the resolved manifest, selects the product
and builds charging controls, the expdb logger, both boot services and policy.
It defaults to eight jobs (`R1_ANDROID_JOBS` overrides this) and accepts explicit
Android build targets as arguments. Logs remain under `/rabbitr1/logs`.

The product has not yet passed this build. Build the full images after resolving
product, HAL and policy errors, then inspect the generated boot header, DT table,
module hashes, super metadata, partition sizes and AVB configuration. The stock layout evidence is
recorded in [stock-android-layout.json](research/stock-android-layout.json).

Keep `OUT_DIR` relative to the Android source root. The pinned Siso build tool
fails to find its generated `main.star` when given an absolute config directory;
`../../out/android` still keeps all output in `/rabbitr1/out/android`.

The pinned vendor image libraries need vendor builds of several dependencies.
The tracked patches enable only the required library variants. Patch
application checks repository commits, patch hashes and exact touched-file
contents before changing anything; it accepts its already-applied changes and
refuses unrecognized edits. A resolved manifest identifies repository commits;
the patch series records the intentional differences from those commits.
Unrelated local edits are preserved; review and record them separately before
a release build.

The [device notes](../android/device/README.md) track the current hardware and
release gates, including missing battery capacity (which can trigger Android's
empty-battery shutdown), Android `expdb` validation, and
the change from stock virtual A/B to dedicated A/B extents. Existing flash and
restore helpers cover the diagnostic package; an Android package needs its own
complete backup, layout and restore validation before distribution.

The [boot-control adapter](BOOT-CONTROL.md) implements the stock A/B record
format, eMMC boot-region selection and clearing the `avbbctl` flag. Its core
and storage boundary have host tests. The product selects its Android and
recovery services; their Soong, SELinux and device checks are still required.

The `r1-expdb` service starts asynchronously after `post-fs` when the fixed build
property `ro.vendor.r1.expdb.enabled=1` is set. It verifies the 20 MiB `expdb`
partition and creates a UUID-owned, single-target mapping of its first 18 MiB.
It checks the active table, device identities and ueventd links before loading
the two fixed pstore modules, then verifies backend registration and parameters.
The final 2 MiB is excluded. The diagnostic mapper shares the same geometry.
The service never formats or erases the partition. Failed setup leaves logging
unavailable and reports the error to the kernel log; Android boot can continue.

The logger's host tests simulate device syscalls and module loading. Run them
with `python3 android/device/logging/tests/run.py`. The kernel's pstore callbacks
use the retained opener credentials so readers, console callers and background
workers do not need raw block-device permissions. Combined policy compilation,
actual node labels, module attachment and persistence still need validation.
Best-effort pstore has no dedicated panic writer, so neither panic persistence
nor logs from before startup are guaranteed.

Recovered files use the `pstore_blk` backend name, for example
`/sys/fs/pstore/console-pstore_blk-0`. Android's current previous-boot liblog reader
expects `pmsg-ramoops-0`; use direct collection or the existing expdb decoder for
these records until that reader is integrated. Do not assume `logcat -L` collects
them.
