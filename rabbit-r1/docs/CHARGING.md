# Charging control

The r1 device tree now connects the MT6357 battery measurements, MT6370 charger
and MediaTek gadget budget to `rabbit-r1-charging`. The charger and policy are
**enabled experimentally**. Physical charging, USB compliance, PMIC interrupts,
ADC calibration and shutdown behavior have not been tested on an r1. This is
not a beta-ready charging implementation.

## Kernel policy

The board starts with a 500 mA battery-current cap. Android restores its saved
choice after persistent properties load. Automatic selects the board maximum
of 1,000 mA; manual choices are 500–1,000 mA in 100 mA steps. These are battery
charge-current limits, not total USB input-current settings or guaranteed speeds.

The policy polls every two seconds and reacts to gadget-budget notifications.
It combines the requested cap with these independent constraints:

* Fresh battery presence, temperature, voltage and current readings are required.
  BATON detection must already be enabled; failed reads never become defaults.
* Normal charging uses at most 1,000 mA and 4.2 V. Below 15 °C or from 43 °C,
  current is limited to 500 mA; the warm voltage limit is 4.1 V. Charging stops
  below 5 °C or at 50 °C, with 2 °C recovery hysteresis. Voltage below 3 V or
  reaching the selected ceiling stops charging; restart requires 100 mV margin.
  These bring-up limits are deliberately stricter than the stock DT's 4.4 V,
  1 A and 0–55 °C envelope. They are not a replacement for battery validation.
* BC1.2 DCP/CDP permit at most 1 A. SDP uses the gadget's actual enumeration and
  suspend budget, capped at 500 mA. Budgets below the charger's 500 mA minimum
  charge current isolate input; no small USB budget is rounded up. The diagnostic
  ACM gadget advertises 100 mA, so it does not charge from an SDP host.
* An optional TCPM supply can provide a confirmed 5 V Type-C/PD budget. It is not
  connected in this board tree: TCPC and higher-voltage negotiation remain off.
* Charger overvoltage, thermal regulation, fault state or safety-timer expiry
  inhibit charging. A ten-hour software session limit latches until observed
  physical disconnection. A separate twelve-hour hardware timer is configured
  and checked for unexpected reset.

Input isolation is requested before shutdown, suspend, invalid telemetry or a
failed transaction. Target changes inhibit charging, program limits, check that
measurements are less than 1.5 seconds old and that the USB budget has not changed,
then enable charging last. A failed operation attempts isolation and preserves
its error. Failed bus writes may have taken effect; an error is not proof that
charging stopped.

`richtek,managed-charging` makes MT6370 start with input isolated. Successful
policy enable calls renew a ten-second software lease. Expiry requests input
isolation and charge inhibition; a failed isolation is retried. Ordinary telemetry
reads do not renew it. The stock hardware watchdog is disabled, as in Rabbit's
initialization: arbitrary I2C traffic feeds that watchdog, so it cannot establish
policy freshness. The software lease cannot protect against a stalled kernel,
a blocked I2C controller or a failed PMIC. Physical safety validation remains
required before release.

The policy uses power-supply controls, not the MT6370 regulator interface, which
controls OTG power output. Device links order supplier unbind; cleanup stops work
and notifications before releasing supply references. Suspend inhibits charging
rather than leaving an unsupervised software policy active.

## Android integration

`android/charging` contains a platform-signed settings app, a native persistence
service, Soong modules, init service and scoped SELinux rules. It adds
**Settings → Battery → Charging speed** through the Battery dashboard category.
The menu shows the saved request, applied charge limit and limiting reason. An
unavailable driver or failed write is displayed explicitly. The app does not
write charger registers or disable thermal/fault protection.

`persist.sys.r1.charge_limit` stores `auto`, `500`, `600`, `700`, `800`, `900` or
`1000`. The daemon retries deferred probes and failed writes, and reads the
kernel cap back each pass so driver rebinds restore the choice. Malformed stored
values fall back to 500 mA. Only the settings app's SELinux domain can set this
property; only the daemon can write the board policy's cap. The exported activity
requires the signature-level `DEVICE_POWER` permission and is primary-user only.

LineageOS 24's existing `ChargingSpeedPreferenceController` uses fixed
`HealthInterface` fast-charge modes. The r1 feature supplies the requested mA
choices through its own Battery dashboard entry. Do not advertise another fast
charge HAL for this board without coordinating its policy ownership.

For an existing LineageOS 24 r1 product under the workspace:

```sh
python3 /rabbitr1/src/mainline/rabbit-r1/scripts/integrate-lineage-charging.py \
    --tree /rabbitr1/src/android
```

The installer copies the feature to `device/rabbit/r1/charging`, adds its product
and BoardConfig includes once, and refuses to overwrite modified feature files.
`--product` and `--board` select other existing makefiles within that Android tree.
Build `R1ChargingSettings`, `r1-charging` and `selinux_policy` in the configured
product. The initial r1 product now includes the charging feature through
`install-lineage-device.py`; see [ANDROID.md](ANDROID.md). Its full build is
pending. The diagnostic boot ZIP contains the kernel policy, not Android or an
installed settings APK.

The kernel ABI is documented in
`Documentation/ABI/testing/sysfs-platform-rabbit-r1-charging`. `r1-report` includes
its coherent status snapshot. Applied values are commanded limits, not measured
current, and are unconfirmed whenever `error` is nonzero.

## Verification

* The native policy harness executes production decisions and transactions under
  ASan/UBSan: 815,304 temperature/voltage/source/current combinations, threshold
  hysteresis, bad caps, sensor/write failures, superseded grants, timer latching,
  suspend failure recovery and sysfs validation. Four faulty policy variants
  are rejected at runtime.
* The MT6370 harness executes the production guard, live health/presence reads,
  lease renewal/expiry/retry and shutdown, alongside the existing current,
  power-path, fault and threaded teardown tests. Battery presence checks cover
  256 register states and 256 read failures.
* Android resources and Java compile against the pinned Android 17 public SDK.
  Native service/JNI sources compile against the corresponding AOSP headers.
  The actual reconciliation function is tested with modeled properties/sysfs,
  including deferred probe, rebind, write failure and corrupt preferences.
  The installer is tested twice to verify idempotent build hooks.
* The kernel objects, complete r1 kernel and DTB are built for AArch64. CI runs
  both new test suites and the existing packaging checks.

Full Android Soong linking, combined SELinux-policy compilation, Binder/Health
integration, on-device Settings behavior, electrical measurements and sustained
charging/disconnect tests remain outstanding. No test above emulates a battery
or establishes hardware safety.

Source references: Rabbit's pinned
[`mt6357-gauge.c`](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/power/supply/mt6357-gauge.c),
[`MT6370 charger`](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/misc/mediatek/pmic/mt6370/mt6370_pmu_charger.c),
and [LineageOS's charging-speed controller](https://github.com/LineageOS/android_packages_apps_Settings/blob/beaa8aa6323be765ad9369ec91ad3604cc65b58a/src/com/android/settings/fuelgauge/ChargingSpeedPreferenceController.java).
Android build inputs and their hashes are in `sources.lock.json`.
