# Power driver investigation

The supplied MT6765 support table marks **MT6357 regulators partial** and
**MT6370 charging working**. These are different devices. The table describes
community testing on MT6765 devices; it is not proof that either works on an r1.

The fenolf-based fork has the same MT6357 regulator and PMIC-wrapper drivers as
the previously inspected evilMyQueen tree. Its additional work therefore does
not itself resolve the reported regulator issue.

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

## MT6357 battery-sense ADC

The upstream `mt6359-auxadc` driver also supports MT6357, but its inherited
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

MT6357 ADC and charging remain disabled in the diagnostic kernel. Probe/reset
error handling, firmware-owned ADC requests, battery-current compensation and
the temperature policy still need work before enabling battery management.
An ADC voltage is not yet a temperature or a fuel-gauge reading.

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
