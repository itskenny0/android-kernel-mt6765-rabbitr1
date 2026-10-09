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
Android battery percentage, status, health and shutdown behavior still require
separate work; passing these counter tests does not establish beta readiness.
