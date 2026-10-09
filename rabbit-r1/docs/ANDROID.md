# LineageOS 24 build integration

The initial r1 product is tracked under `android/device`; the charging UI and
service remain under `android/charging`. The native build and full policy build
are not verified yet. No Android image has been produced or added to the
mtkclient package.

`android/local_manifests/r1.xml` pins the common mainline device tree and its six
dependencies. Use it with the LineageOS manifest commit in `sources.lock.json`.
The upstream manifest uses the Android 17 release tag for AOSP projects and
moving Lineage branches for other projects; record a resolved manifest after
sync before treating an Android build as reproducible.

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
export GOCACHE=/rabbitr1/.cache/go-build GOPATH=/rabbitr1/.cache/go
export GRADLE_USER_HOME=/rabbitr1/.cache/gradle ANDROID_USER_HOME=/rabbitr1/.cache/android
cd /rabbitr1/src/android
set +eu
source build/envsetup.sh
lunch lineage_r1 trunk_staging userdebug || exit
m R1ChargingSettings r1-charging android.hardware.boot-service.r1 android.hardware.boot-service.r1_recovery selinux_policy
```

These are the next validation commands, not a claim that the product already
passes them. Build the full images after resolving product, HAL and policy
errors, then inspect the generated boot header, DT table, module hashes, super
metadata, partition sizes and AVB configuration. The stock layout evidence is
recorded in [stock-android-layout.json](research/stock-android-layout.json).

The [device notes](../android/device/README.md) track the current hardware and
release gates, including missing battery capacity (which can trigger Android's
empty-battery shutdown), Android `expdb` startup, and
the change from stock virtual A/B to dedicated A/B extents. Existing flash and
restore helpers cover the diagnostic package; an Android package needs its own
complete backup, layout and restore validation before distribution.

The [boot-control adapter](BOOT-CONTROL.md) implements the stock A/B record
format, eMMC boot-region selection and clearing the `avbbctl` flag. Its core
and storage boundary have host tests. The product selects its Android and
recovery services; their Soong, SELinux and device checks are still required.
