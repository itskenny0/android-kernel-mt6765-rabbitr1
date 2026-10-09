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

The importer verifies pinned source hashes and the original full capture hash,
compares all four DT tables with captured temperature endpoints, and records
hashes of generated data. The original source replay also captures cutoff/RAC
behavior for research; those results are excluded from the C implementation's
pass criteria. The frozen fixtures and host sanitizers verify software behavior,
not electrical calibration or SOC accuracy on a device.

Remaining estimator work
------------------------

The stock cutoff/RAC Qmax path is deliberately a separate implementation step.
It iterates 100 entries after constructing only 53, so its unconstructed zero
rows affect cutoff results. It also permits zero capacities and missing search
brackets. The C core uses only explicit valid rows and does not introduce a
zero-voltage tail or an assumed RAC measurement to imitate those cases.

A usable-capacity policy must accept verified cutoff/load/impedance inputs,
define bounded bracket handling, and account for that documented boundary
difference. A real estimator additionally needs trustworthy OCV initialization,
battery/profile identity, a paired charge baseline, reset/removal detection,
continuity checks and defined invalidation. Charger FULL at the current
conservative voltage limit must not manufacture a 100-percent seed.

This library publishes no CAPACITY property and changes no kernel driver, DT,
Health service, charging limit, RTC byte or hardware accumulator. Those remaining
integration decisions and real device validation are required before exposing
a battery percentage to Android.
