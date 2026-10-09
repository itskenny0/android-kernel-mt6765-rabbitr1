# Power driver investigation

Current charging integration: [CHARGING.md](CHARGING.md). The charger and board
policy are now enabled experimentally; earlier driver-only stages below record
their previous disabled state. Android control sources, persistence and build
hooks are implemented, but no full Android image or physical charging test exists.

The supplied MT6765 support table marks **MT6357 regulators partial** and
**MT6370 charging working**. These are different devices. The table describes
community testing on MT6765 devices; it is not proof that either works on an r1.

The fenolf-based fork has the same MT6357 regulator and PMIC-wrapper drivers as
the previously inspected evilMyQueen tree. Its additional work therefore does
not itself resolve the reported regulator issue.

## MT6765 MFG bus protection

Patch 0079 adds the stock MFG protection sequence: assert INFRACFG bit 25,
then bits 21 and 22, polling each acknowledgment before SRAM power-down;
release the masks in reverse order without polling clear acknowledgments.
SCPSYS uses the existing INFRACFG syscon at `0x10001000`.

Probe checks the required command and status register maps for every domain
it will register before powering any domain. A missing INFRACFG map previously
could alias a discovered SMI map at index zero. Missing maps now reject probe;
valid legacy mappings and omitted domains remain supported. The old DT cannot
provide working power domains without the required resources.

[Source pins and checks](../tests/gpu/mt6765-bus-protection.json) record 35 host
controls plus six additional controls, an ARM64 translation-unit compile and
board DT compilation. These are offline checks. The provider powers MFG during
probe and may power it down at late init, so this change has runtime effects
even without a GPU node. Physical power sequencing remains untested.

The generic helpers still ignore individual register-write errors. Shared
VCORE voting, mux order, hardware APM/CORE0 ownership and GPU enablement remain
separate work; no voltage, OPP, GPU node or firmware change is included.

## Corrections implemented

All register evidence below comes from Rabbit's published driver and register
definitions at `8167c8c1087f057d2ef302fc93b47554291687ec`:

* [Vendor regulator driver](../src/kernel/drivers/regulator/mt6357-regulator.c)
* [Vendor register definitions](../src/kernel/include/linux/mfd/mt6357/registers.h)
* [Modified mainline driver](../src/mainline/drivers/regulator/mt6357-regulator.c)

### SRAM selector write mask

The original mainline entries pass `0x7f00` as the voltage **write** mask for
`MT6357_LDO_VSRAM_CON0` and `MT6357_LDO_VSRAM_CON1`. The vendor defines
`RG_LDO_VSRAM_{PROC,OTHERS}_VOSEL_MASK = 0x7f`, shift **0**. Thus the write mask
is `0x007f`. The monitored voltage in `LDO_VSRAM_*_DBG0` uses shift **8** and
must retain `da_vsel_mask = 0x7f00`.

The patch separates these correctly. A requested selector such as `0x2a` must
update bits 6:0 in the write register; a readback of `0x2a00` decodes to `0x2a`.
This is a source-backed correction, not confirmation of the wiki issue's cause.

### MRV SRAM register mapping

Rabbit's driver tests `RG_TOP2_RSV0[15]`, whose address is `TOP2_ELR0` (`0x015a`).
When set, it exchanges the hardware register banks associated with VSRAM_PROC
and VSRAM_OTHERS. Mainline did not implement this.

The patch reads that bit once per probe and exchanges the enable, voltage-write,
voltage-readback and status register addresses. The masks are identical between
the two SRAM regulators; logical IDs, names and voltage ranges stay attached to
their consumers. Each PMIC gets a private copy of the descriptors, preventing a
probe/reprobe from changing a global table. Read errors propagate before any
regulators are registered. There are no direct PMIC writes in this helper.

Whether a particular r1 contains this revision has not been measured.

### Hardware status readback

`is_enabled` reads the software enable control. The new `get_status` reads the
DA enable indication used by Rabbit's driver:

| Type | Register family | Mask |
| --- | --- | --- |
| Bucks | `BUCK_*_DBG1` | bit 0 |
| SRAM LDOs | `LDO_VSRAM_*_DBG1` | bit 0 |
| Other LDOs | `LDO_*_CON1` | bit 15 |

VCN33 BT/Wi-Fi share one hardware status register. An ON indication is not a
voltmeter reading and cannot prove the expected voltage reaches the board.
This patch makes `/sys/class/regulator/regulator.*/status` useful alongside
`state`, `microvolts` and the regulator debug summary in `r1-report`.

## Checks and limits

`python3 scripts/test-mt6357.py` compiles the production tables and helpers in a
host harness with a read-only fake regmap. It independently derives expected
status registers/masks from the vendor header and checks all 32 entries, both
SRAM write/read masks, normal/MRV mappings, unchanged unrelated descriptors,
ON/OFF readback and bus-read failures. The test rejects a copy with the original
SRAM write masks restored. The AArch64 kernel build checks integration with the
real regulator/regmap interfaces. Neither check emulates the PMIC.

The bring-up configuration disables CPU frequency scaling, idle and suspend;
it does not exercise SRAM voltage transitions. `clk_ignore_unused` and
`regulator_ignore_unused` retain firmware state for initial observation and
must be removed after consumers and critical rails are verified.

The next power experiment needs a stock and mainline boot log, the live FDT,
regulator state/status/voltage summaries, the PMIC revision indication, and
measurements of the affected rail. First establish PMIC-wrapper transfers,
interrupts and consumer/supply bindings. Then test one rail at a time with its
known voltage constraints. No charging-current increases, guessed voltage
changes, or raw register write commands are included in this workspace.

MT6370 charger integration remains separate work: confirm the r1 I2C address,
GPIO interrupt routing, ADC and USB/charger detection, battery limits and thermal
policy. The stock config enables the MT6370 charger/backlight and MT6357 battery
drivers. Copying generic nodes from another MT6765 handset is insufficient.
The [I2C investigation](I2C.md) records the r1 wiring and fixes the inherited
clock-divider mismatch. Actual bus transfers and charging remain untested.

## MT6370 input-current measurement

Two inherited charger bugs are corrected before enabling this device on r1.
They are independent of the community report about MT6357 regulators.

The charger used ADC channel ID 5 as an index into the array returned by
`devm_iio_channel_get_all()`. That array contains only the channels requested by
the consumer's `io-channels` property. The binding's own example, also used by
the Nokia ROO tree, requests only IBUS. The array then contains one channel and
a sentinel, so accessing element 5 is out of bounds. The driver now searches
the sentinel-terminated list for the MT6370 IBUS channel with current type and
retains that channel. Probe fails if IBUS is absent, before charger settings
or interrupts are initialized. Compact, reordered and complete ADC lists work.

The MIVR workaround also compared `iio_read_channel_processed()` with a
100,000 microamp threshold, although IIO current readings are in milliamps.
For example, a 500 mA reading incorrectly satisfied `500 < 100000`, causing
the workaround to toggle the CFO control above its intended 100 mA threshold.
The driver now requests a scaled reading in microamps, retaining fractional
milliamp precision, and checks that the reading is nonnegative and below
100,000. This only corrects measurement and workaround selection; it does not
raise charging limits or implement battery-temperature policy.

The source evidence is `drivers/iio/inkern.c`, the MT6370 ADC driver's
`mt6370_adc_read_scale()`, `include/linux/iio/consumer.h` (array termination),
and `Documentation/ABI/testing/sysfs-bus-iio` (current units). The original
charger code is also visible in the
[driver submission](https://lists.infradead.org/pipermail/linux-arm-kernel/2022-August/766568.html).

`scripts/test-mt6370.py` runs the production channel-selection and MIVR work
functions with IIO/regmap stubs. It covers a lone IBUS channel, missing/wrong
channels, every position in the list, the legacy full list, both sides of the
100 mA threshold, fractional readings and failures. IRQ re-enabling and wake
reference release are checked on every path. Restoring positional indexing or
the old unscaled-reading behavior makes the test fail. These are host tests;
the actual ADC calibration, IRQ delivery and charger operation remain untested.

## MT6370 writable limits

The charger previously passed signed power-supply values into an unsigned
range helper. A request of `-1` selected the maximum register value: 5 A for
charge current, 4.71 V for charge voltage, 3.25 A for input current, 13.4 V for
the minimum-input-voltage setting, and 850 mA for precharge/termination current.
These results were reproduced with the original setter and the kernel's actual
linear-range helpers; no device was accessed. A 500 mA charge-current request
also silently selected the driver's 900 mA minimum.

The setter now rejects negative values with `-EINVAL` and nonnegative limits
outside its supported range with `-ERANGE`, before any register write.
`ONLINE` accepts only 0 or 1 before changing attachment state or queuing work.
In-range quantization is unchanged: minimum input voltage rounds upward;
current limits and charge voltage round downward. Register-write errors still
propagate to callers. Zero is rejected for these six limits; it is not a
charge-disable operation.

These are generic driver bounds, not the r1 battery's approved charging limits.
MT6370 and RT5081 now support 500 mA using the stock workaround described below.
The other supported variants retain their existing 900 mA minimum. Charging
remains disabled in the device tree while battery constraints, input-power
limits and temperature policy are unfinished.

`test-mt6370-limits.py` compiles the production property setter, ONLINE handler,
register/range tables and kernel range helpers with ASan/UBSan. It checks
3,744 valid-limit writes/bus failures, 57 rejected requests and six ONLINE
transitions, including range endpoints, adjacent values, field mapping,
rounding and preservation of neighboring bits. Ten regression variants fail
the runtime assertions. Regmap writes, locking and work queuing are modeled;
these checks do not establish charger operation or electrical safety.

### MT6370 low-current workaround

Rabbit's active `mt6370_pmu_charger.c` disables VSYS short protection below
900 mA on MT6370/RT5081 and restores it at 900 mA or above. It enters hidden
mode with the four-byte key `96 69 c3 3c`, changes bits 6:5 of register 0x36
to 0x00 or 0x40, then closes hidden mode. Mainline's banked regmap addresses
are 0x107 for the key and 0x136 for that register. The vendor source, PMU
header and charger header are pinned in `sources.lock.json`. The nearby IEOC
adjustment is inside `#if 0` in the active stock driver and is not copied.

The mainline driver checks the model before selecting its minimum current.
On MT6370/RT5081 it serializes the entire transition, first closes any inherited
hidden gate, then opens it, sets the target protection state, closes it and
programs current. It programs the target protection on every request; the
stock cached-current assumption would be unreliable after LK or a bus error.
There are no other hidden-mode users in the current mainline MT6370 drivers.
Any future user must share serialization at the MFD level.

After an error it attempts to close the gate and inhibit charging through the
ramp-down helper described below, retaining the first error and logging cleanup
failures. A failed bus transaction can
leave hardware unchanged or partially changed, so charging cannot be guaranteed
off when that disable fails. Successful current writes never re-enable it.
Probe initializes locks, work items, model limits and hardware settings before
publishing power-supply callbacks; managed cleanup drains work before releasing
the power supply and workqueue.

`test-mt6370-current.py` compiles the production helpers, setter, initialization
and probe with the kernel's range helpers. It checks 257 identification cases,
816 current transitions, 66 invalid requests, 246 fault/recovery cases, 400
threaded transitions and 60 probe/unwind cases. The bus model covers partial
passcodes, failed writes that take effect, failed cleanup, stale protection and
an inherited open gate. Fifteen faulty variants fail runtime assertions.
ASan/UBSan and pthread locks check the host model; physical protection behavior,
charging current and thermal response still require a device.

### Charge enable and inhibit

MT6370 and RT5081 now expose the standard `charge_behaviour` property with
`auto` and `inhibit-charge`. Readback follows the hardware enable bit; `auto`
means charging is permitted, not that current is flowing. The other supported
PMIC models do not advertise this property because their stop sequence has not
been established here. Unsupported modes and models return an error before I/O.

Rabbit's active `mt6370_enable_charging()` lowers ICHG to 500 mA before clearing
CHG_EN to avoid a VSYS overshoot. It waits 2 ms per 50 mA of reduction. The
shipped kernel contains the same sequence. Mainline now derives the wait from
the **actual current selector**, rather than a cached request. From 1 A this
is 20 ms; the supported 5 A maximum takes 180 ms. It holds the current mutex
across the reduction, wait and disable. The temporary reduction leaves the
hidden protection setting unchanged, following the stock stop sequence.

An already disabled charger needs no ramp. Inherited selectors at or below
500 mA can be stopped directly. An unrecognized selector above the supported
range returns `-ERANGE` and still attempts to clear charge enable. Failed
reads and failed ramp writes also lead to a disable attempt. A ramp write
reporting failure may have reached hardware, so the computed wait is retained.
The first error survives later cleanup. Failed I/O can still leave charging
active or prevent the intended ramp; software cannot guarantee an electrical
shutdown in that condition.

The driver retains the last successful current **request** separately from
hardware readback. Inhibiting may leave the current register at 500 mA; `auto`
restores the request and its model-specific protection before enabling. A
current-programming or inhibit/enable I/O failure invalidates that request for
resume. A fresh successful current-setting operation is then required; it
never enables charging by itself. Invalid numeric requests leave the previous
configuration intact. Current and behaviour reads share the transition mutex,
and power-supply notifications cover successful changes and failed operations
that may have changed hardware state.

On these two models, probe inhibits charging before changing initial settings.
It does not enable charging on successful registration. Shutdown and managed
teardown close the power-supply API, mask and synchronize every successfully
registered IRQ, drain driver work, then attempt to inhibit charging. This runs
before IRQ and power-supply resources are released. Partial IRQ setup failures
use the same path. A fallback managed inhibit runs again after power-supply
removal, while the regmap and current mutex still exist.

A separate API mutex lets admitted property calls finish before shutdown closes
the gate; later reads and writes return `-ESHUTDOWN`. It is released before IRQ
synchronization because an IRQ handler can call the property setter. Status
reads use the internal ONLINE helper to avoid recursively acquiring that mutex.
Canceled pending MIVR work balances the handler's IRQ mask and wake reference;
running work completes its own cleanup. Repeated shutdown calls are harmless.
Other PMIC variants quiesce callbacks and work without applying an unestablished
charge-disable sequence.

BC1.2 work checks that registration has published the power-supply handle before
notifying it; early ONLINE writes must not cause a NULL-handle notification.
The core supplies its initial notification after registration.

This driver property is a control mechanism, not the Android UI's **Automatic**
preference and not a complete charging policy. The future policy service must
validate battery measurements and faults, apply battery/USB/current/voltage
limits, then choose whether charging may run. The generic driver current range
still extends to 5 A. The r1 charger node remains disabled while that service,
watchdog handling and hardware validation are unfinished.

The current harness compiles the actual control helpers, property callbacks,
per-model descriptor, probe, cleanup and BC1.2 notification path. It compares
normal stop writes and delays with the compiled Rabbit stop routine. Tests
cover 256 inhibit/readback cases, 257 resumes, 73 rejected requests, 385 control
fault/recovery cases, 520 reads and 600 threaded control/current operations,
in addition to the current-transition cases above. The bus model permits a
failed write to take effect and a failed read to overwrite its destination.
Twenty-one control regressions and the existing 15 current regressions fail
runtime assertions. Locks, time, I/O and device resources are modeled; neither
VSYS transients nor real charge-enable behavior has been measured.

The shutdown tests invoke the production `platform_driver.shutdown` callback.
They cover 18 model/work cases, 910 rejected property calls after shutdown,
12 bus-failure cases and five synchronized property/shutdown races. The actual
IRQ registration code is exercised through partial lookup/request failures.
IRQ synchronization, running work and managed resources are modeled; these
checks reject 21 faulty variants but do not establish behavior during a physical
reboot. USB enumeration/suspend budget integration remains unfinished.

### Initial USB input limit

Probe programs IAICR to 100 mA and enables its regulation loop before selecting
it as the input limit. It then waits at least 5 ms before disabling the external
ILIM pin constraint. The interval follows Rabbit's stock initialization; the
order ensures the replacement limit exists before the pin constraint is removed.
Every failed transaction stops initialization before callbacks are published.
This replaces reliance on inherited firmware settings, including an AICR loop
that may have been disabled.

The [RT5081 register table](https://www.richtek.com/assets/product_file/RT5081/DS5081-00.pdf),
page 72, describes IAICR, its loop-enable bit and the input selector. Their
locations agree with the pinned Rabbit MT6370 headers. The manufacturer PDF is
currently indexed but its download returns 404; the stock sources remain the
reproducible local reference.

The production initialization runs against all 256 inherited input-register
values and all four selector values on six supported models: 6,144 cases.
Another 48 fault cases check error propagation and ensure the pin constraint
is not removed early, including failed writes that take effect. Probe tests
also inject initialization failures before callback publication. These are
register and timing models, not measured input-current limits. Twelve faulty
variants fail runtime assertions; the pre-fix driver reproduces the missing
initial limit.

This 100 mA starting point does not handle USB suspend or authorize charging.
The gadget requests budgets below the charger's 100 mA minimum, including 2 mA
at suspend; those requests must not be rounded upward. Connecting the USB budget
to the charger and validating input-path isolation remain required before
enabling the r1 charger node. Battery-current preferences cannot override that
input budget.

### Zero input budget and restoration

On MT6370/RT5081, writing `0` to `input_current_limit` requests power-path
isolation. Positive requests below 100 mA still return `-ERANGE`; the policy
must deliberately choose isolation for such budgets. Other PMIC variants keep
their existing range and reject zero because this sequence is not established
for them.

The operation first sets FORCE_SLEEP, before waiting for work or the charge
ramp. It then masks and synchronizes the MIVR IRQ, drains its delayed work,
inhibits charging and sets MIVR to 13.4 V. The register and power-path sequence
come from Rabbit's [active driver](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/misc/mediatek/pmic/mt6370/mt6370_pmu_charger.c#L2059).
The shipped `mt6370_enable_power_path` at `0xffffff80089342d0` also selects
physical register 0x11, bit 3. This is distinct from HZ, bit 2. Mainline preserves
the first error and continues the remaining stop attempts; stock overwrites
some errors and updates its cached state even after failed transactions.

The software request remains suspended after any error. `charge_behaviour=auto`
cannot release it. Readback checks the physical FORCE_SLEEP bit: it reports
zero when that bit is set, otherwise the programmed IAICR limit, and propagates
read errors. This is register state, not a measurement or proof that USB draw
has reached zero. Failed writes can leave the input path active.

When input is suspended, a valid positive request reasserts isolation, stops charging,
programs the input budget, enables its loop, selects IAICR and restores the
last successful MIVR request before clearing FORCE_SLEEP and unmasking MIVR.
Charging stays inhibited until policy explicitly permits it. A positive limit
change on an already active input path preserves the charging state. While suspended,
voltage-limit writes update the stored restore target and leave hardware MIVR
at its maximum. Probe captures inherited MIVR and sleep state before exposing
callbacks. IRQ registration shares the API lock, so an early zero request is
honored when the MIVR IRQ becomes available.

The host harness exercises 386 input transitions, 262 fault/recovery cases,
46 rejected budgets, 774 input reads and 53 early-publication/inherited-state
cases. It includes pending/running MIVR work, failed writes that take effect,
failed recovery cleanup, inherited sleep, invalid MIVR selectors, raw readback
that differs from the software request, and input requests racing shutdown.
Twenty-eight faulty input-path variants fail runtime assertions.
Bus transactions, IRQ execution, scheduling and elapsed time are modeled.
Actual input isolation, USB suspend timing and battery/system-rail behavior
still require hardware measurements. The USB budget source below is not yet
connected to charging policy, and the charger node remains disabled.
With that node disabled, charger settings remain inherited from earlier firmware.

### USB gadget power budget

The MediaTek MUSB controller now exposes a read-only power supply named
`<controller>-gadget`, normally `11200000.usb-gadget` on r1. The r1 kernel enables
`CONFIG_USB_MUSB_MEDIATEK_POWER_SUPPLY`. `current_max` reports the gadget's USB 2
request in microamps, `online` indicates a nonzero request, and `scope=Device`
keeps Linux's system-supply helper from counting this constraint source as
external power. The Android health implementation must also exclude it when
reporting plugged-in state. The supply uses the USB controller's firmware node
so policy can reference it by phandle.
It does not measure VBUS or identify a charger, cable, Type-C Rp or PD contract.

This fixes a missing software path: MUSB previously passed each request to the
generic USB PHY, whose unknown charger type and absent `set_power` callback
left no usable budget notification. The new optional MUSB platform callback
retains the PHY call and publishes requests through the power-supply core's
queued notifications. Callbacks take only a spinlock; they do not access the
charger or wait for a consumer. Other MUSB platforms retain their PHY path.

A request below the current budget reduces the published limit before calling
the PHY. An increase is published only after that callback succeeds. Requests
are sequenced so an older completion cannot replace a newer request, including
suspend or disconnect. PHY errors withdraw the budget. Requests above 500 mA
also withdraw it and return `-ERANGE`. Leaving peripheral mode clears the
budget; returning requires a fresh request. Removal and shutdown permanently
close the source before controller/PHY release, and managed cleanup precedes
power-supply unregistration. The disabled build option preserves the original
PHY behavior.

The source retains the gadget's units and values: composite suspend requests
2 mA, disconnect requests zero, and the MUSB reset path ends with an 8 mA
request. Configured requests follow the gadget configuration; the diagnostic
ACM configuration uses 100 mA. These values must not be rounded up to the
MT6370's 100 mA input minimum. Charging policy must choose input isolation when
an applicable budget is below that minimum. It must also distinguish an SDP
budget from a separately established CDP, DCP or Type-C/PD allowance. No such
classification or policy enforcement is implemented by this source.

`scripts/test-musb-budget.py` compiles the production property, registration,
budget, dispatch, role-switch, controller-exit, removal and shutdown callbacks.
It checks 1,011 requests, seven invalid budgets/roles, ten PHY/probe failures and eight
supersession interleavings, including a synchronized two-thread case. It also
checks registration-time reads, notification coalescing, error propagation,
cleanup order, and both enabled and disabled configurations. Nineteen faulty
variants fail runtime assertions; both AArch64 configurations compile. PHY, MMIO,
notifications and device resources are modeled; timing, real USB enumeration
and physical current draw remain untested. `r1-report` includes the new supply's
uevent once the controller probes.

### Android charging-speed setting

The implemented Settings entry, persistence service, kernel policy, build hooks
and remaining device tests are described in [CHARGING.md](CHARGING.md).

## MT6357 battery-sense ADC

The `mt6359-auxadc` driver in this fork supports MT6357, but its inherited
tables and conversion code had several problems. The corrections use Rabbit's
published `drivers/iio/adc/mt635x-auxadc.c`, its MT6357 register definitions, and
the shipped v0.8.293 device tree:

* ISENSE requests bit 1, VCDT bit 2, and DCXO temperature bit 4 of `RQST0`.
  The old entries requested bits 0, 0 and 10 respectively. ISENSE has 15 data
  bits, not 12. Register offsets above ADC36 are not consecutive hardware
  channel numbers; the existing output addresses for DCXO/VCORE/VPROC are
  correct and stay unchanged.
* The ordinary channels measure voltage. Battery-temperature conversion needs
  a thermistor model; ISENSE is a voltage input, not a computed battery current.
  MT6357 now exposes these as `IIO_VOLTAGE`. This changes their incorrect
  `in_temp`, `in_current` or `in_resistance` sysfs names to `in_voltage` names.
* The shared scale now includes ADC resolution:
  `raw * ratio_numerator * VREF_mV / (ratio_denominator * 2^bits)`.
  A half-scale 12-bit, 1:1 input is 900 mV at the 1,800 mV reference. The old
  scale would report 3,686,400 in the channel's claimed units. This scale
  correction also applies to the driver's other PMIC models.
* VBIF is exposed as binding ID 13; existing IDs are unchanged. Conversion
  temporarily clears `BATON_TDET_EN` at `0x1236[1]`. DCXO conversion selects
  the AP input at `0x1216[4]`. Both save and attempt to restore the prior bit
  under the ADC mutex, including failed conversions; a failed restoration
  returns an error instead of a reading. Unrelated bits are preserved.
* The former VDCXO entry had no MT6357 descriptor and could use zero-valued
  fields as register addresses. It is no longer advertised; its binding ID
  remains reserved. Rabbit's MT6357 table provides no validated mapping for it.

Sampling errors and failed stop writes now propagate to callers. The shared
external-input path attempts to release its pull-up after failed selection,
sampling or stopping. Timeout recovery stays inside the ADC mutex so one read
cannot reset hardware while another conversion is running. These changes do
not guarantee recovery when the PMIC bus itself has failed.

`test-mt6357-auxadc.py` compiles the production tables, chip callback selection
and conversion functions with ASan/UBSan. It derives 12 mappings from pinned
Rabbit sources and checks raw values, voltage units, fractional scales, delayed
readiness, repeated timeouts, cleanup and unrelated-bit preservation. It covers
227 conversion/cleanup cases plus 48 injected mux bus errors, including writes
that take effect despite reporting an error. Thirteen faulty variants are
rejected by runtime assertions. Polling, regmap operations and locking are
modeled; the impedance-conversion routine and electrical accuracy are not
covered. CI separately compiles the AArch64 object.

Reset now checks each bus operation. It attempts to release reset after a
failed assertion and to relock protected writes after a failed unlock or
reset. The first failure is returned. MT6357 then repeats the two requests in
Rabbit's reset table: AP channel 7 at `0x110e[7]` and GPS DCXO at `0x111a[10]`.
The driver's common `RQST1` index denotes silicon `RQST2` on MT6357, so it
cannot be used for the GPS request. Other PMIC models keep their existing
request behavior.

Probe refuses to register the ADC after a failed reset. A runtime reset
failure also blocks conversions until a later reset succeeds, under the same
mutex. These paths are covered by 37 additional reset/probe scenarios,
including failed writes that do or do not reach the device, protection-key
cleanup, multiple failures and recovery. The harness compiles the actual
probe and reset functions; allocation, registration and the bus are modeled.
Eleven deliberately broken reset variants fail the runtime assertions; the
13 conversion regressions still fail as well.

MT6357 ADC is now enabled for the battery measurements described below. Charging
control remains disabled pending policy integration. Raw ADC voltages remain
distinct from converted battery temperature. Physical reset timing and interaction
with firmware requesters remain untested.

### Impedance voltage sampling and binding IDs

MT6357 now has its own impedance-conversion callback. Rabbit's driver sets
`IMP_CG0` software-mode bit 0, then enable bit 1, then `IMP1` auto-repeat bit
15. It waits for `IMP0` bit 8 and reads ADC33 before stopping. The shutdown
sequence pulses `IMP0` bits 14/7, clears auto-repeat, clears software mode,
and leaves enable bit 1 set. The inherited MT6358 callback cleared both clock
bits. The MT6358 and MT6359 callbacks remain separate.

Every MT6357 start, poll, data-read and cleanup operation now checks the bus
result. All five cleanup writes are attempted after a partial start or a
failed read. A failed cleanup marks the ADC as needing reset, so later reads
cannot start until reset succeeds. The caller receives the first failure and
no output value. Successful results contain only the 15 data bits, excluding
the ready flag. The hardware's impedance mode supplies a battery-voltage
sample; it does not calculate battery current or resistance. Current requests
are rejected instead of returning a fabricated zero.

`test-mt6357-imp.py` compiles the actual MT6357 callback, registered callback
selection, reset and IIO read functions with ASan/UBSan. It compares the start
and stop operations with the pinned Rabbit driver and register fields. It
covers 48 conversion cases, 71 bus-failure/timeout cases, 21 recovery cases,
status-bit masking and preservation of unrelated fields. Clearing the modeled
conversion destroys its data, which checks that the read happens first.
Twelve faulty variants are rejected by runtime assertions. Regmap operations,
locks and readiness are modeled; these results do not establish electrical
accuracy or real PMIC timing.

The common firmware-channel lookup now matches binding IDs to `channel`
fields instead of using IDs as array indices. On MT6357, VBIF ID 13 would
otherwise be rejected, while DCXO ID 9 would select VBIF. Reserved or absent
channels and malformed specifiers return `-EINVAL`. The ADC harness covers
440 lookups across all five PMIC tables, including reversed tables, wide IDs
and incorrect cell counts. Six lookup regressions fail the runtime checks.

## MT6357 battery current

The r1 configuration now includes `BATTERY_MT6357` and a gauge node with the
stock 10 mOhm shunt and unity current calibration. It exposes signed
`current_now` in microamps under `/sys/class/power_supply/mt6357-battery/`;
positive means charging, negative means discharging. `r1-report` includes this
power-supply data. This is experimental current sensing, not a capacity or
charging-policy implementation.

The reference is Rabbit's active `drivers/power/supply/mt6357-gauge.c` and
`drivers/power/supply/mtk_battery.c`, selected by `CONFIG_BATTERY_MT6357`.
The older files under `drivers/misc/mediatek/pmic/` and
`drivers/power/supply/mediatek/battery/` are not this firmware's active path.
The shipped kernel's symbols and disassembly corroborate the selected latch
and current-conversion routines.

Current is latched through `FGADC_CON1` at 0xd0a and read from 0xd8a. Its
16-bit sample is ones' complement: both 0x0000 and 0xffff mean zero. The
conversion preserves stock rounding in 0.1 mA before correcting for shunt
resistance and current gain, then reports microamps. Stock DT `R_FG_VALUE=10`
becomes internal 100 and `CAR_TUNE_VALUE=100` becomes internal 1000. Copying
the raw DT values into the driver's internal formula would give wrong results.
The mainline binding instead specifies microohms and gain in parts per thousand.
Missing, zero, overflowing or unrepresentable calibration is rejected at probe.

The driver retains firmware's measurement-engine configuration. It checks the
engine enable bit and analog/digital clock gates before sampling; stopped
hardware returns `-EAGAIN`. It clears inherited latch state before its first
sample and serializes reads. Each latch poll has a 20 ms timeout with a 100 µs
interval; those are initial software bounds, not measured hardware timings.
Every error attempts the full latch-release sequence. Failed release blocks
publication and forces recovery before another sample. Failed recovery returns
an error instead of reading potentially stale data. No charger, coulomb-counter
reset, measurement-engine enable or clock settings are written.

`test-mt6357-current.py` checks 786,432 conversions against compiled stock
arithmetic, 60 sampling/readiness cases, 3,903 fault/recovery/timeout cases,
200 threaded reads and 16 probe cases. Sixteen faulty variants are rejected.
It compiles the production callbacks and actual kernel polling macros with
ASan/UBSan; bus behavior, registration, time and hardware readiness are modeled.
Electrical accuracy, retained firmware calibration and boot-time engine state
still require a device. The auxiliary ADC now supplies voltage and temperature
inputs; charger control remains disabled pending policy integration.

### Battery voltage and temperature

The gauge now exposes `voltage_now` in microvolts and `temp` in tenths of a
Celsius degree. `MEDIATEK_MT6359_AUXADC` is built in for the MT6357 ADC node.
These are experimental measurements; they do not enable the charger or
establish a working Android health service. `r1-report` falls back to individual
power-supply readings if one failed property prevents a complete uevent.

The board's named inputs follow the shipped DT: **ISENSE** for battery voltage,
BAT_TEMP for thermistor voltage, and VBIF for its measured pull-up reference.
ISENSE is a voltage channel, despite its name. VBIF uses binding ID 13 in
mainline, whereas Rabbit's driver used 14. The gauge resolves channels by name
and requires voltage type; it propagates deferred probe and missing-input
errors before publishing any power-supply callbacks. The old current-only
configuration still works when no ADC inputs are specified.

Temperature uses the stock 16,900-ohm pull-up and 10 mOhm return-path correction.
Signed current is truncated to integer milliamps, multiplied by the return-path
resistance and truncated to integer millivolts. That drop is subtracted from
**both** measured voltages. The corrected thermistor voltage is then converted
to resistance using the corrected reference. The board supplies the active
21-point, 10 kOhm NTC table from Rabbit's `mtk_battery_table.h`; its exact bytes
also occur in the shipped v0.8.293 kernel. Interpolation is linear in resistance,
with the stock -40/60 °C endpoint clamps. The generic driver validates table
length, increasing temperatures, decreasing positive resistances and pull-up
parameters rather than embedding the r1's thermistor curve.

Every property obtains fresh measurements. Failed thermistor, reference or
current reads return their error without changing the caller's value. Zero,
negative, reversed or unrepresentable voltages are rejected; there is no
nominal-reference or zero-current fallback. Temperature acquisition includes
current-latch cleanup errors. Samples are sequential, not simultaneous, and
neither electrical calibration nor accuracy during current transients has
been established. The vendor ADC's diagnostic filter/reset path is not copied;
it does not supply an independently validated temperature sample.

The current harness also compiles the actual voltage/temperature acquisition,
conversion and ADC probe code. It checks 589,127 temperature/boundary results,
including every 12-bit thermistor code at five references and 19 signed currents,
plus every ohm across the stock curve. Expected temperatures come from compiled
stock conversion routines and their compensation block. Another 32 sample
cases, 41 fault/input cases and 51 ADC probe cases cover positive IIO success
returns, failed reads that overwrite private buffers, channel types, deferred
probe, malformed calibration, immediate registration callbacks and integer
extremes. Twenty-four temperature/ADC regressions and the existing 16 current
regressions are rejected. ASan/UBSan and modeled IIO/regmap interfaces check
software behavior; these are not hardware measurements.

### Battery policy recovered from stock

The shipped `/charger` node selects a 4,400,000 µV normal charge voltage and
1,000,000 µA AC charge/input limits; USB uses 500,000 µA. Its JEITA table stops
charging below 0 °C or at/above 55 °C and lowers the configured current/voltage
to 500,000 µA and 4,200,000 µV above 45 °C and below 55 °C. Hysteresis must
be carried over with the transition logic. These are recovered stock settings,
not limits newly approved by hardware tests.

The gauge node provides a 16,900-ohm pull-up and a nominal 1,800 mV reference.
Rabbit's battery code reads VBIF and compensates the temperature-sense voltage
using measured battery current before applying its thermistor table. The
MT6370 driver's initial 4,450,000 µV setting is distinct from the charger
manager's policy; copying that driver default would not reproduce stock
charging. The extracted nodes are recorded in `out/r1-battery-stock-inputs.json`.

## r1 power monitor

I2C5 and the MT6370 MFD/ADC are now enabled in the r1 board file. The bus uses
the stock push-pull mode and pin muxes, at 100 kHz for initial transfers.
GPIO11 carries the PMIC interrupt; GPIO41 is reserved in the disabled Type-C
node. `MEDIATEK_MT6370_ADC` and `SENSORS_IIO_HWMON` are built into the kernel.

`r1-report` reads the hwmon labels and values for VBUSDIV5, VSYS, VBAT, IBUS,
IBAT and TEMP_JC. Voltage and current are reported in mV and mA; temperature
is in milli-degrees Celsius. TEMP_JC is the **PMIC junction temperature**, not
battery temperature. TS_BAT is not exposed as a thermometer, and this does not
provide battery capacity, a fuel gauge or Android health data.

Probing the MFD checks its vendor ID and initializes its interrupt controller;
the ADC driver resets its conversion register. Reading a measurement starts an
ADC conversion. These are normal driver operations, not a passive bus sniffer.
The regulator child registers the four rails without board voltage/enable
constraints; the bring-up `regulator_ignore_unused` parameter retains firmware
state. The charger, Type-C, backlight, indicator and flashlight child nodes are
explicitly disabled so those drivers do not change their hardware settings.
This does not guarantee or manage whatever charging state LK left behind.

The MT6370 and hwmon schemas pass for these nodes. A compiled-DTB test compares
the wiring with the stock image and rejects a changed IRQ or enabled unfinished
child. Full-board schema validation still has the previously recorded 28
diagnostics. Actual bus transfers, interrupt routing and measured ADC accuracy
require the device; none are inferred from a successful build.

The [backlight investigation](BACKLIGHT.md) fixes register masks, brightness
mapping, shutdown polarity and reserved-bit handling before panel integration.
The board backlight remains disabled; this does not change charging behavior.

## Persistent logs

The stock log partition is `expdb` (20 MiB). The mainline expdb boot profile
uses pstore through a device-mapper view of its first 18 MiB. Rabbit's
`drivers/misc/mediatek/log_store/log_store.c` defines `EXPDB_LOG_SIZE` as 2 MiB
and writes at `file_size - EXPDB_LOG_SIZE`; its header offset is also bounded
within that tail. Our mapper excludes those last 2 MiB. This replaces old dump
contents in the first 18 MiB and uses the Linux pstore format, not AEE.

This adds diagnostics, not an eMMC or charging fix. The logger checks the
partition label and size, then loads matching `pstore_zone` and `pstore_blk`
modules after storage appears. The generic backend has no dedicated panic
writer. Early crashes and storage failures still require UART. See
[FLASHING.md](FLASHING.md) for setup, raw log extraction and restoration.
