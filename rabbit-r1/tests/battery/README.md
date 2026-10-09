# MT6357 charge counter evidence

Run `/rabbitr1/toolchains/boot-tools/bin/python /rabbitr1/src/mainline/rabbit-r1/scripts/test-mt6357-charge-counter.py --replay-stock`.
The host tests use GCC, ASan/UBSan and pthreads. Instruction replay additionally
requires Unicorn from the boot-tools environment and the extracted
`/rabbitr1/firmware/stock-v0.8.293/merged.dtb`; a missing or mismatched DTB fails
replay. The default suite runs with `python3` without `--replay-stock` and does
not read the DTB, so it can run before stock metadata extraction in source CI.
It still compares production functions to the captured machine-code results
and the hash-verified stock C function.

`mt6357-stock-coulomb.json` contains 2,664 outputs captured by executing the stock
ARM64 `coulomb_get` function, with its exact instructions and provenance hashes.
Only external register reads, latch helpers, tracing and debug-level lookup are
mocked in that replay. The stock result is signed tenths of a milliamp-hour;
the fixture multiplies it by 100 to express microamp-hours. Its columns are the
packed 32-bit hardware sample, mainline shunt in micro-ohms, gain in permille,
and the captured result in microamp-hours. Replays check both register addresses,
their order, the helper order and the return value.

The stock merged DTB has `R_FG_VALUE = 10` and `CAR_TUNE_VALUE = 100`; stock
`mtk_battery.c` multiplies both by ten, producing internal 0.1-milliohm and
permille units. The existing mainline r1 DTS has 10,000 micro-ohms and gain 1,000.
No new calibration value or charging configuration is introduced.

The host harness extracts the actual production conversion, latch release,
shared acquisition and property functions. It compares every packed magnitude
and sign against the hash-verified stock C arithmetic, in addition to the
independently captured outputs. Its register model exercises failures before,
during and after the two-half read, cleanup recovery, deadline boundaries,
coherent snapshots while the live counter changes, and competing current and
counter readers. The polling macros are extracted from the actual kernel.
The existing `test-mt6357-current.py` continues to test the full probe and the
current, voltage, temperature and presence paths.

`CHARGE_COUNTER` is the signed hardware accumulator, not remaining charge or
percentage. It can wrap or reset; this increment neither tracks those events
nor initializes a state-of-charge estimate. No physical-device test has occurred.
Android battery percentage, health and shutdown behavior still require separate
work; passing these counter tests does not establish beta readiness.


# MT6357 battery status

Run `python3 /rabbitr1/src/mainline/rabbit-r1/scripts/test-mt6357-status.py`.
This runs the counter suite first, then reuses its actual production current and
counter functions and independent PMIC transaction model. The STATUS harness
extracts the production status read, worker, blocking notifier, teardown,
supplier initialization and probe. It also executes the real MT6370 online and
status functions against mocked attachment/register states. ASan/UBSan and
pthreads exercise error propagation, registration callbacks, failed probe,
repeated writes, policy feedback, periodic and immediate transitions, teardown
while a supplier read or entered notifier blocks, static descriptor lifetime,
and competing status/current/counter reads.
Framework workqueues, a blocking notifier chain, device links, devres, OF
references and bus reads are
modeled; these are not physical-device or kernel scheduler tests. The existing
current/ADC suite separately covers the real ADC initialization.

The r1 gauge references only `r1_charger` through `power-supplies`. Without that
optional reference the generic gauge does not advertise STATUS. A reference
and a device link retain the supplier and order consumer teardown before
supplier driver data disappears. Work starts only after battery registration
and its managed stop action. A blocking notifier accepts only property changes
from the bound charger. The stop action first disables scheduling, unregisters
and synchronously drains that notifier, then drains work before unregistering
the battery. Early registration events and events after stopping cannot start
work. Descriptors and property arrays have static lifetime, so a power-supply
class iterator retaining the supply after driver unbind cannot dereference a
freed descriptor. The descriptor has no external-power callback. Measurement
and status scheduling use separate mutexes, and supplier I/O holds neither.

Reads return the actual supplier STATUS and propagate its errors without
publishing a stale value. Invalid status enums fail with ERANGE. Notifications
occur on the initial observation, a status change, or a change between valid
and failed reads. Changing one read error into another does not trigger a new
notification, but direct reads still return the exact error. Repeated charger
writes can notify without changing state; filtering prevents those events from
cycling through the r1 charging policy and battery. The worker refreshes every
five seconds to observe charger transitions without an IRQ, and supplier events
request an immediate refresh. A newly queued event is not postponed by the
periodic refresh.

For the current MT6370 driver, detached means DISCHARGING; attached READY/FAULT
means NOT_CHARGING, charge-in-progress means CHARGING and charge-done means FULL.
FULL describes the charger's termination state under the active configuration;
it does not establish 100% of a calibrated battery capacity. Attachment state
comes from the existing charger attachment path. No charging settings, IRQs,
CAPACITY, HEALTH or shutdown policy are changed. Charger input faults and chip
thermal regulation are not interpreted as battery health. Runtime attachment,
notification latency, teardown ordering and real charge completion remain
hardware validation requirements. STATUS alone does not resolve the missing
capacity estimate or make Android firmware beta-ready.

# MT6357 retained RTC bytes

`python3 scripts/test-mt6357-rtc-nvmem.py` tests the actual read-only RTC provider,
probe and alarm operations with the separately pinned stock register mapping.
See [RTC-NVMEM.md](../../docs/RTC-NVMEM.md) for the byte layout, optional compiled
DT/config checks, lifetime behavior and unresolved retained-state validity.

# MT6357 live collector

`python3 scripts/test-mt6357-live.py` compiles the actual complete gauge body with
ASan/UBSan for diagnostics disabled and enabled. It reuses the existing stock
pin checks, polling macros, calibration and probe fixture, then adds paired
current/counter latch and raw ADC models. No hardware is accessed. See
[MT6357-LIVE.md](../../docs/MT6357-LIVE.md) for interpretation, reference/locking
rules, error publication and the remaining runtime estimator work. The suite
checks acquisition completeness, not SOC acceptance or physical coherency.
