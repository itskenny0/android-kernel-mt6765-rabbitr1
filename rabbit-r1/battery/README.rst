haretic battery profile math
===========================

This directory implements checked, portable C arithmetic for the stock r1
battery0 curves. It performs no I/O and holds no global estimator state. A
successful calculation establishes a mathematical result for supplied inputs;
it does not establish that a boot voltage or retained counter is a valid seed.

The four stock curves contain exactly 53 rows each, at 50, 25, 0 and -10 degrees
Celsius. ``stock_profiles.c`` comes from the verified stock merged DTB. The
Qmax metadata comes from the verified stock source defaults, because those
properties are absent from that DT. Their provenance and the captured stock
function hashes are recorded in ``provenance.json``.

API and units
-------------

``profile.h`` provides fixed-size data structures and error-returning functions.
It uses signed 64-bit arithmetic, no allocation and no userspace library calls.
A conditional type alias permits the same source to use Linux's ``s64`` type
when eventually integrated into a kernel; that integration is not part of this
change. All failed operations leave caller-owned outputs unchanged.

The numerical units match the stock algorithm:

* Charge and usable capacity: 0.1 mAh, or 100 microamp-hours.
* OCV: 0.1 mV, or 100 microvolts.
* Cell resistance: 0.1 milliohms, or 100 micro-ohms.
* DOD and internal SOC: 0.01 percent, with 10000 representing 100 percent.
* Temperature supplied to profile construction: whole degrees Celsius.

``r1_battery_whole_celsius`` explicitly truncates mainline TEMP's tenths of a
degree toward zero. Thus -0.9 C becomes 0 C, and -9.9 C becomes -9 C. This choice
preserves a whole-degree input to the stock math; it is not an instruction
capture of a mainline temperature adapter.

``r1_battery_profile_at`` validates every table, requires equal explicit row
counts and strictly ordered temperatures, clamps to the outer temperatures,
and interpolates charge, voltage, resistance and both Qmax metadata values.
Ascending and descending table order produce the same cold-anchored rounding.
Repeated charge and voltage coordinates are retained. Resistance may vary
non-monotonically. An interpolated profile has no usable capacity assigned.

``r1_battery_assign_dod`` requires an explicitly supplied positive usable
capacity, then assigns ``charge * 10000 / usable`` to each valid row. The stock
Qmax metadata is never selected implicitly. Values above 10000 are retained
without the stock unsigned-short wrap. In-place normalization is supported.

``r1_battery_ocv_to_dod`` and ``r1_battery_dod_to_ocv`` use the first row crossing
the requested coordinate. Outside-curve inputs select the corresponding
endpoint. A nonpositive OCV is rejected. For the -10 C curve normalized with the
explicit 920.0 mAh fixture, DOD 10004 selects 3.6990 V; DOD 10005 selects the
last 3.4000 V point. Collapsing repeated rows would change that stock behavior.

``r1_battery_update`` recomputes the reference OCV's DOD using the supplied
current-temperature profile, then subtracts the signed relative-charge term.
Positive charge means charging and reduces DOD. Its internal SOC is
``10000 - DOD``. Neither output is restricted to the UI percentage range.
The caller must supply delta charge in the documented units; raw mainline
CHARGE_COUNTER values are in microamp-hours. Conversion and counter-baseline
continuity belong to the future estimator integration.

``r1_battery_calculate_usable`` takes a temperature curve and explicit minimum
voltage, discharge load, RAC, shunt resistance, meter resistance and DC ratio.
Its result contains initial cutoff/capacity, final cutoff and a profile normalized
by the final usable capacity. Existing DOD/usable fields are ignored. No Qmax
metadata or fixed capacity supplies a missing input. Input and output may share
the result's profile field; failure leaves the complete output untouched.

Load is in 0.1 mA; RAC, shunt and meter resistance are in 0.1 milliohms; DC ratio
is percent (100 means unity). Minimum voltage and DC ratio must be positive.
Load and resistance inputs must be nonnegative. Explicit zero inputs support
no-load/ideal-resistance arithmetic tests; zero is never a missing-measurement
fallback. For example, 5000 load units and 1500 RAC units mean 500 mA and
150 milliohms, giving a 750-unit (75 mV) initial voltage allowance.

The stock source has a unit inconsistency at its RAC boundary:
``mt635x-auxadc.c:auxadc_get_rac`` returns milliohms, and
``mt6357-gauge.c:ptim_resist_get`` forwards that value unchanged, but
``mtk_battery_algo.c:fgr_construct_vboot`` divides ``iboot * rac`` by 10000.
With the verified 0.1-mA current unit, that division requires 0.1-milliohm RAC.
This API names the latter unit explicitly. A future caller with a verified
milliohm measurement must convert it with checked multiplication by ten.
The tests pass the explicit arithmetic operand to stock instructions; they do
not claim that stock's unconverted supplier input is dimensionally correct.

Cutoff and resistance search
---------------------------

The calculation first adds ``load * RAC / 10000`` to minimum voltage and
interpolates usable charge at that cutoff. It then normalizes DOD, searches
from low voltage for a strict loaded-voltage crossing, and derives final usable
charge and DOD from the resulting OCV coordinate. Cell resistance is scaled by
DC ratio before adding shunt and meter resistance. Both signed stock divisions
are retained: ``comp = (-load * total_resistance) / 1000`` followed by
``(comp + 5) / 10``. This asymmetric negative-current rounding is intentional;
it is not replaced with a single division or symmetric rounding.

Capacity interpolation requires nondecreasing charge and nonincreasing voltage
across all valid rows. It uses the first *strictly lower* voltage crossing and
anchors interpolation at that lower-voltage row, preserving stock truncation.
Repeated voltage/charge rows are retained: strict crossing guarantees different
voltage coordinates when interpolation is needed. Below the final valid voltage,
capacity is the last valid charge. A zero result (including at/above a zero-charge
first row) returns ``R1_BATTERY_NO_CUTOFF``. Resistance need not be monotonic;
the search retains stock's backwards first-crossing order and does not assume
that loaded voltage is globally monotonic or suitable for binary search.

The search starts at the upper DOD of the selected bracket and decrements by
10 units (0.1 percent). It accepts only a strict loaded-voltage crossing on that
grid. A missing crossing or repeated selected DOD coordinate returns
``R1_BATTERY_NO_CUTOFF``. It does not read row -1 or return the final unsuccessful
sample, both of which occur on captured stock paths. A bracket spanning more
than 10000 DOD units returns ``R1_BATTERY_SEARCH_LIMIT``. This documented
computational bound permits at most 1001 samples; it is not a physical assertion
that every input outside that range is impossible.

Stock constructs 53 rows but scans 100 for cutoff capacity. The zero tail
changes its initial normalization. This implementation uses the explicit count
(up to 53), so those unconstructed rows cannot influence any result. Comparison
fixtures deliberately repeat the last valid stock row in the extra slots to
exercise equivalent endpoint behavior; separate captures preserve the actual
zero-tail differences. Final DOD remains wide instead of wrapping at stock's
unsigned-short store. The fixture captures both the stock pre-store register
and the stored value to make that difference independently verifiable.

All subtraction, multiplication, division and addition intermediates are
checked. Division truncates toward zero. A zero divisor, invalid profile,
inconsistent DOD normalization or unrepresentable intermediate returns an
error instead of a substitute percentage. An intermediate overflow is rejected
even when a wider-than-64-bit implementation could calculate a final result.

Evidence and tests
------------------

Run from the installed workspace::

    source /rabbitr1/scripts/env.sh
    python3 /rabbitr1/battery/tests/run.py

Use ``--out /rabbitr1/out/battery-review/usable-capacity/test`` to redirect test
artifacts. Both suites run by default.

The normal test requires neither stock firmware nor emulation. It compiles the
actual C sources with strict warnings, ASan and UBSan. Frozen expected values
were captured from the stock kernel's ARM64 instructions, independently of this
C implementation. The runner only serializes those captured values for the C
test executable; it does not calculate expected interpolation or charge results.

The fixture contains 81 complete temperature profiles, 2358 OCV/DOD conversions,
8 explicit-Qmax normalizations, 66 repeated-coordinate boundary conversions and
308 signed charge updates. Additional cases cover malformed profiles, ascending
table order, missing normalization, signed unit conversion, integer overflow,
unchanged outputs on errors and temperature-dependent reference reconstruction.
The supplied-Qmax fixtures isolate arithmetic. They do not claim to reproduce
the configured stock QMAX_SEL=1 usable-capacity estimator. Normal stock boot
uses a userspace daemon; the captured open kernel algorithm serves its fallback
and recovery path. These captures do not establish identical daemon behavior.

With the verified stock firmware, kernel ELF, symbol list, original sources and
Unicorn environment present, the instruction captures and imported data can be
regenerated with::

    /rabbitr1/toolchains/boot-tools/bin/python /rabbitr1/battery/tests/replay-stock.py
    /rabbitr1/toolchains/boot-tools/bin/python /rabbitr1/battery/tools/import-stock.py
    /rabbitr1/toolchains/boot-tools/bin/python /rabbitr1/battery/tests/replay-usable-capacity.py

The importer verifies pinned source hashes and the original full capture hash,
compares all four DT tables with captured temperature endpoints, and records
hashes of generated data. The dedicated usable-capacity replay runs the actual
``fgr_construct_vboot`` QMAX_SEL=1 instructions and nested cutoff/resistance
functions. It reuses pinned profile captures, verifies the kernel ELF and
function hashes, and mocks only explicit RAC and unrelated hardware/logging
inputs. ``tests/usable-provenance.json`` independently pins this replay and
its frozen output; normal tests require neither firmware nor Unicorn. Expected
arithmetic comes from emulation, including the quotient before the stock
unsigned-short DOD store. Invalid-index and no-crossing execution paths become
explicit C rejection fixtures, with unchanged-output assertions. Additional
host tests cover signed overflow, invalid physical parameters, duplicate selected
DOD, search bounds, first-crossing behavior and in-place publication. The cutoff suite checks 931 successful instruction
paths (including four explicit-tail boundaries), 108 rejected stock negative-row
paths and 18 rejected search fallthroughs. Eighteen successful paths also expose
the stock unsigned-short DOD wrap.

The frozen fixtures and host sanitizers verify software behavior, not electrical
calibration or SOC accuracy on a device.

Remaining estimator work
------------------------

The pure cutoff calculation is now available, but no measured RAC or runtime
usable-capacity policy is supplied. A real estimator still needs trustworthy OCV
initialization, battery/profile identity, a paired charge baseline, reset/removal
detection,
continuity checks and defined invalidation. Charger FULL at the current
conservative voltage limit must not manufacture a 100-percent seed.

This library publishes no CAPACITY property and changes no kernel driver, DT,
Health service, charging limit, RTC byte or hardware accumulator. Those remaining
integration decisions and real device validation are required before exposing
a battery percentage to Android.
