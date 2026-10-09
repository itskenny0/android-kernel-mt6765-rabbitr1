# MT6357 bracketed live measurements

The private `mt6357_gauge_collect_live()` function acquires current and charge
counter together before and after six battery ADC readings. This supplies
observable live inputs for a future model estimator. It does not calculate
capacity, accept an SOC seed, choose motion limits, change charging, reset the
gauge, or claim that terminal voltage is measured open-circuit voltage.

`CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS` defaults to `n`. The r1 bringup fragment
and defconfig enable it for one acquisition/report during each successful
resource-initialization attempt, before power-supply registration. An acquisition
error is reported but does not abort otherwise valid registration. Failed
registration may therefore leave one diagnostic report. Disabling the option
removes this diagnostic acquisition and logging; the internal collector remains
available to a future caller in this driver. There is no DT switch, module
parameter, sysfs file, exported getter, or new power-supply property.

## Acquisition and interpretation

The existing gauge mutex covers the entire sequence:

1. Check engine/clock state, recover an inherited latch if necessary, request
   one FG latch, read CURRENT, CAR low/high and BATON, and release the latch.
2. Read ISENSE, BAT_TEMP and VBIF raw ADC values and scale metadata.
3. Read VBIF, BAT_TEMP and ISENSE in reverse order.
4. Acquire and release a second paired CURRENT/CAR latch with BATON.
5. Calculate each thermistor temperature with that set's bounding current,
   using the existing calibrated NTC function. No third current read occurs.

BATON is a separate detector register, not asserted to be electrically latched
with CURRENT/CAR. Its raw state and errno are retained for both ends. PRESENT
is valid independently of current/counter conversion: bit 0 must enable the
detector; bit 1 then indicates absence. A disabled detector yields `-EAGAIN`.
A successful observation of absence is value zero with errno zero; collection
success is not a claim that a battery is suitable for a seed.

The by-value report contains no device/devres pointers. All unattempted fields
have `-ENODATA`. A failed transport may overwrite its output argument, but the
report stores raw values only from successful reads. Preparation, latch and
release statuses are separate. Preparation includes any required inherited
latch release; `release_error` is the cleanup of the newly attempted latch.
A primary read error and a later cleanup error are both retained. Converted FG
values are withheld unless the entire paired transaction, including cleanup,
succeeded. Current conversion and charge conversion each retain their errno.

If the first FG pair fails, ADC acquisition does not start. After an ADC error,
remaining planned ADC fields and the final FG bracket are still attempted.
The aggregate error selects one failure; callers must inspect individual field
statuses for complete provenance. Detector errors are considered after the
acquisition and conversion path, so an earlier BATON error can coexist with a
later ADC error returned by the collector. Every completed attempt publishes an
initialized diagnostic report even on failure. This differs intentionally from
the portable math API's unchanged-output-on-error contract. A null report
pointer returns `-EINVAL` without I/O. Success means complete acquisition, not
stable load, model accuracy or an accepted seed.

The first diagnostic line gives driver-binding generation, per-binding sequence,
boottime interval and aggregate errno. FG reports show begin/ready/end times,
preparation/latch/release errors, current in microamps, charge in microamp-hours,
presence and temperature in deci-C. Subsequent `wordN=raw/errno` lines map to
engine, clock, CURRENT, CAR-low, CAR-high, BATON (indices 0–5). ADC set/channel
indices identify ISENSE, BAT_TEMP and VBIF (0–2), with raw code/errno,
scale-format:numerator/denominator, scale errno and converted integer mV/errno.
Zero values next to nonzero errnos are placeholders, not measurements.

Current is positive for charging. CHARGE_COUNTER is a signed accumulator since
hardware reset, **not remaining capacity**. Existing stock sign/special-code
handling and staged rounding are unchanged. Board shunt/gain and thermistor
calibration are reused. The supported ADC descriptor is unsigned and offset-free,
with `IIO_VAL_INT` raw results and positive `IIO_VAL_FRACTIONAL` scale:

| Input | Raw width | Scale to mV |
| --- | --- | --- |
| ISENSE | 15 bits | 5400 / 32768 |
| BAT_TEMP | 12 bits | 1800 / 4096 |
| VBIF | 12 bits | 1800 / 4096 |

Raw and scale errors stay separate. Unsupported formats/offsets, out-of-range
raw codes, nonpositive scale terms and unrepresentable conversion results fail.
Integer-mV truncation deliberately matches the existing provider and NTC path.
The strict collector does not use IIO's generic raw-to-processed fallback, which
can substitute the raw value when scale lookup fails.

Boottime timestamps bound software observation intervals, including mutex wait
and suspend time. They do not timestamp instantaneous electrical samples. A
binding generation distinguishes instances while this module is loaded; it is
not persistent and does not identify silicon counter resets. Sequence overflow
is rejected. Reports from earlier calls remain independent copies.

## Provider lifetime and locks

The supported optional ADC wiring is the named OF `io-channels` binding used by
the r1. Each input resolves its `io-channel-names` index and corresponding phandle
from the gauge's own node. `of_find_device_by_node()` obtains an independently
referenced platform supplier. The driver rejects a self-reference, then uses
`device_trylock()`; busy, unbound or already-unbinding suppliers defer probe.
Both `device_is_bound()` and `DL_DEV_DRIVER_BOUND` are checked under that lock:
unbind can temporarily release the device mutex while its old driver still
counts as bound. A nonblocking acquisition avoids waiting on an unbind that
itself waits for consumer probe.

While the supplier is locked and bound, a managed
`DL_FLAG_AUTOREMOVE_CONSUMER` link is created **before** IIO channel lookup.
The returned channel must name that same supplier parent and pass IIO's protected
type query. A retained old IIO instance is rejected. The temporary OF node and
platform references are balanced on every path. The three r1 inputs share one
supplier; repeated `device_link_add()` calls coalesce into one actual link.
Other provider topologies, unnamed inherited inputs and firmware-independent
IIO maps are outside this bounded ADC binding.

Do not derive lifetime protection by taking a reference through an arbitrary
`channel->indio_dev->dev.parent`: IIO's retained device reference does not keep
its parent reference after `device_del()`. Nor may the caller dereference the
returned managed-link pointer as a presence proof. The referenced OF lookup and
locked supplier state establish the link before those hazards can occur.

Driver core waits for a linked consumer's probe and unbinds active consumers
before supplier resources are removed. IIO raw/scale/type APIs separately guard
callbacks with `info_exist_lock`; an entered callback drains before `info` is
cleared, and a subsequent operation fails `-ENODEV`. The initial collector runs
synchronously inside probe, whose lifetime owns gauge resources. There is no
new asynchronous callback. A future in-driver worker must be stopped/drained
before its measurement resources are released. A retained power-supply object
alone is not sufficient ownership for an exported collector.

The measurement order is gauge mutex → IIO info lock → AUXADC mutex → regmap.
Every FG latch is released before IIO. No supplier device lock is held during
collection; it is used only for probe resource acquisition. The AUXADC provider
never calls back into the gauge. Existing temperature properties release IIO
locks before obtaining the gauge mutex, so they introduce no inverse nesting.
Other ADC users can interleave between collector fields; intervals expose the
additional elapsed time, without claiming exclusive ADC access. Charger status
notifications/work use their existing separate mutex and are not invoked here.

Managed links order teardown; they do not promise automatic re-probe. A later
binding acquires fresh channels and a new generation. The current provider's
read path may invoke its existing timeout recovery, including AUXADC reset.
This patch adds no reset decision or control operation; failed observations
remain failed. Existing FG read/release control writes remain necessary for
ordinary measurement and are the only new collector-direct writes.

## Verification and remaining work

Run `python3 scripts/test-mt6357-live.py` from the project tree. The host suite
compiles the unmodified production gauge body with real kernel polling macros
and mocked register/IIO/OF/device resources, under ASan/UBSan. Both diagnostic
configurations cover paired latch generations and ordering, every successful
trace bus operation failing, primary-plus-cleanup errors, inherited recovery,
engine/clock/timeouts, raw poisoning, unsupported/failed scales, all 40,960
possible codes of the three actual ADC inputs, stock NTC association, absent
and disabled detectors, sequence exhaustion, by-value reports, blocked parallel
current reads, entered IIO callback removal, raw-success/scale-removal, probe
failures, supplier state/rebind/identity and balanced reference cleanup.

The independent ADC expectation is exact integer `raw*numerator/denominator`;
stock current/NTC functions come from the separately pinned vendor sources.
The existing charge-counter suite retains its 2,664 captured ARM64 outputs and
exhaustive stock-C comparison. No new combined-latch hardware oracle is claimed:
the combined transaction is checked against a latch-aware register model.
The current/temperature, counter, STATUS and AUXADC suites remain required
regressions. Host device-core/IIO lifetime models are not execution of the
kernel scheduler; source review and the separately replayed core state helpers
support the lifetime argument. Root coordinates a full kernel build and patch
series replay before integration.

No physical device has been tested. Endpoint agreement cannot exclude an
intervening transient, hidden counter reset or sensor error. Runtime seed work
still requires explicit motion/duration/model-agreement limits, counter alignment
and physical validation. This collector supplies real acquisition software; it
does not publish CAPACITY, bypass Android shutdown, validate health, or establish
firmware beta readiness.
