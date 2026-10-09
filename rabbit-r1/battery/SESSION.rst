haretic live battery session
===========================

``session.c`` is the pure runtime path from a complete MT6357 live observation
to a model-estimated SOC and subsequent charge-counter tracking. It uses the
verified battery0 profiles and the existing loaded-profile solver. It performs
no I/O, writes no gauge state, supplies no fixed percentage, and does not publish
kernel CAPACITY. Its explicit policy has no production defaults. Physical error
budgets and runtime availability still require validation before beta use.

Contract and units
------------------

Zero-initialize one caller-owned ``r1_battery_session`` and serialize calls to
``r1_battery_session_step``. State contains only values, with no device pointer,
allocation, global mutable state or borrowed profile. Apart from initialization,
callers must not edit its internals. Arguments and result must not overlap.
The temporary profile lives in a separate, non-inlined frame. Actual ARM64
GCC 13.3 Kbuild with ``__KERNEL__``, ``W=1``, ``-Werror`` and stack-usage output
compiled all three sources and linked their aggregate object without diagnostics.
The largest individual frame was the candidate helper at 1952 bytes; session
step used 960 bytes and read 224. This checks target headers, linking and individual
frames, not a full call-chain stack bound, full kernel module or runtime behavior.

A ``step`` FRESH result is valid as of its ``accepted_ns`` timestamp. Runtime
publication must call ``read(actual_now_ns)``; it must not publish a delayed step
result directly. A cached midpoint is not a new electrical measurement.

The observation has binding generation, capture sequence, boottime start/end,
two endpoints, a collection errno and an optional explicit power/lifetime event.
Each endpoint carries raw packed CAR, converted CAR in microamp-hours, current
in microamps (positive charging), voltage in microvolts, temperature in deci-C,
and independently valid PRESENT. The adapter folds every field/conversion/latch
release error into the endpoint error, preserving the detailed collector report
separately. Populated numerical fields do not override a nonzero error.

This bounded adapter accepts the actual collector's integer domains: current is
a multiple of 100 uA, voltage of 1000 uV, CAR of 100 uAh. It does not truncate an
unsupported finer input. Raw CAR must fit unsigned32; a special magnitude must
have converted zero and ordinary sign must agree with converted sign. It does
not repeat the calibrated counter conversion or pretend these checks prove the
adapter's calibration. Generation, sequence and timestamps must fit positive
signed64. A new binding can restart sequence, but boottime cannot go backwards.
Module/process recreation must initialize a new session rather than reuse an
old generation namespace.

``step`` returns an API error only for null arguments, malformed policy or an
unknown event, leaving caller outputs and state unchanged. Rejected measurement
or arithmetic is an ordinary successful **state transition** with an explicit
reason and detail errno. The result distinguishes:

* ``UNAVAILABLE``: no accepted estimate exists; numerical fields are unusable.
* ``FRESH``: an accepted observation, or a cached estimate whose age-expanded
  uncertainty still satisfies the supplied policy.
* ``HISTORICAL``: an old estimate exists, but is not publishable as capacity.

An invalid observation immediately ends continuity and freshness. It never
manufactures zero, retains an unchanged percentage as fresh, or refreshes the
last accepted timestamp. History retains the actual previous values and epoch.
A modeled zero is valid data and is preserved as zero; failure is represented by
validity, not by reserving a percentage. API errors require the adapter to mark
its cache unavailable too; unchanged-output-on-programmer-error is not a license
to keep publishing the old value as fresh.

Every limit is a supplied assumption
----------------------------------

``r1_battery_policy`` separates model parameters, measurement limits, uncertainty
and publication width. There are no hidden fallback values:

* Explicit reference discharge load, cutoff voltage, shunt, meter resistance
  and DC ratio. These are independent of the signed instantaneous load used for
  the live seed. A positive discharge magnitude becomes negative model current.
* Maximum capture duration, gap between accepted capture ends, epoch age and
  cache lifetime. Equal-to-limit observations are accepted. Epoch age and gap
  violations discard continuity and permit a new, complete live seed.
* Observed absolute-current ceiling and endpoint voltage/current/temperature
  motion limits. Both endpoint temperatures must map to the same whole-C
  profile, within -10..50 C **before** truncation/clamping.
* A separately justified absolute-current envelope over the entire unsampled
  interval, model/sensor charge error per endpoint, cutoff-model charge error,
  and CAR-delta error over the bounded epoch. These error budgets include
  calibration and quantization; they are not derived from endpoint agreement.
* Maximum accepted charge-interval and SOC-interval widths. SOC units are basis
  points (0.01 percent), with 10000 representing 100 percent.

The whole-window current envelope must cover unseen transients. It cannot be
inferred from maximum endpoint current, their equality or net CAR movement:
opposing transients can cancel. Model error must cover the nonsimultaneous ADC,
current and thermistor/reference readings, integer-mV conversion, whole-C
truncation and profile/model error. A charge allowance padded around one exact
root does **not** mathematically enclose all roots introduced by a V/I/T error
box near flat/nonmonotonic regions. That allowance is an externally validated
model assumption, not a rigorous inverse uncertainty calculation or confidence
level. Tests verify policy arithmetic; they do not calibrate these assumptions.

Verified active stock reference inputs are 33500 in 0.1 mV, discharge5000 in
0.1 mA, shunt100 and meter75 in 0.1 mOhm, and DC100 percent. The source default
per-temperature minimum/load arrays, stock DT resistance parser scaling and
profile provenance establish these operands, not accuracy on a mainline device.
The four profiles contain exactly 53 rows at 50/25/0/-10 C, and stock profile ID
selection returns battery0. Pack identity remains an assumption for an original
r1 pack; an arbitrary replacement pack is not validated. Qmax metadata, a raw
LK RAC string, charger FULL, retained RTC data and old power-on voltage never
supply a missing live input or error budget here.

Seed and counter state
----------------------

Both endpoint V/I pairs must have a unique root on the complete temperature
curve. Missing, multiple, flat or discontinuous roots fail. A returned floored
charge coordinate ``q`` becomes a conservative closed interval
``[100*q, 100*q+100]`` uAh. Add the explicit model error and
``ceil(current_envelope_uA * capture_duration_ns / 3.6e12)`` on each side.
This bounds each model root at the end CAR timestamp without pretending an ADC
has a precise latch time. Intersect the endpoint intervals; never average a
disagreement. In-window CAR movement must also satisfy the explicit envelope
plus CAR-delta error. Successful collection alone cannot accept a seed.

Use the independent reference discharge/cutoff to determine usable charge on
that same profile. A first downward crossing or explicit conservative zero-width
cutoff boundary is allowed; ``PROFILE_END`` is rejected because it only denotes
a truncated curve. Expand the floored cutoff by its 100-uAh rounding interval
and explicit cutoff error. Its lower bound must remain positive. A cutoff
boundary is not a seed root or a measured battery capacity.

Store the accepted discharged-charge interval directly and anchor it to the
same report's final CAR. Never round-trip it through OCV/DOD: the stock 0 C
rows38/39 both have OCV37590 at distinct q7604/7804. Within an epoch:

::

    discharged_now_uAh = discharged_seed_uAh - (CAR_now_uAh - CAR_seed_uAh)

Positive CAR means charging. Absolute-baseline subtraction preserves the uAh
coordinate without repeatedly truncating to 0.1 mAh. Shift the seed interval by
that delta and add the supplied bounded-epoch CAR error once, rather than
accumulating it per call. The new complete observation must independently pass
all seed checks and overlap this prediction. Intersect the intervals; a counter
rate violation or empty intersection invalidates the observation and requires a
later fresh seed. There is no same-sample averaging/reseeding after these faults.
Step and whole-epoch CAR movement are both checked against the declared envelope.

Freeze the profile temperature and reference usable-capacity interval for one
epoch. Policy, whole-C profile, binding, age/gap or inter-observation raw-code
class changes establish a new live seed. This deliberately avoids an unvalidated
transport between temperature-dependent charge axes. Reconstructing the same
curve for cross-checking does not change the coordinate. The state tracks a
local session epoch, not a PMIC reset counter.

The raw CAR magnitude occupies bits30:11 with sign31. Both magnitude0 and0xfffff
convert to zero for either sign. A within-bracket sign/special-class transition
rejects the bracket; an inter-observation transition ends continuity and permits
a new seed. Stable special encodings can anchor a fresh live seed. The low11 raw
bits are retained but do not define a converted-code class. This policy rejects
some legitimate zero crossings; it is not a detected-reset claim and never adds
a guessed modulus. Unchanged codes or small plausible deltas cannot prove that
a reset did not occur. BATON endpoints cannot observe every remove/reinsert.

Known suspend, reset, removal or engine-off events immediately invalidate the
epoch. Each event carries a real, monotonic binding generation and boottime
interval, advancing an invalidation-time barrier without fabricating a capture
sequence. A capture must start **strictly later** than the event end: equal
clock values cannot prove whether acquisition started before the invalidation,
even if the capture finishes later. A queued pre-event capture cannot seed
afterward, even if its sequence had never been processed. Ordinary adjacent
captures may still share an end/start timestamp when no event barrier intervenes.
Only a subsequent complete live bracket can reseed without resetting hardware. Malformed/backwards event
metadata cannot establish this barrier and blocks all observation seeding until
a valid dated event supplies it. A later sequence alone cannot clear that
condition; a genuinely new driver binding owns a newly initialized session.
There is no trustworthy persisted hardware epoch in collector0074. A future
worker must invalidate across suspend/resume and all known power transitions,
not silently carry CAR through an unobserved interval. Hidden reset/removal risk
is a remaining physical/runtime limitation, not repaired by arithmetic tests.

Publication and cached reads
----------------------------

The representative charge and usable capacity are interval midpoints. Evaluate
``10000 * (usable - discharged) / usable`` with checked signed64 arithmetic.
SOC bounds round outwards across all four charge/capacity endpoint combinations,
including negative or above-cutoff charge coordinates. The representative SOC
is floored and clamped to0..10000; bounds remain unclamped so uncertainty is
visible. No lower confidence endpoint is silently substituted for a zero SOC.
Integer Android CAPACITY rounding is left to the reviewed kernel adapter; this
API reports basis points and imposes no hidden whole-percent policy.

``r1_battery_session_read(state, now, out)`` is the publication check. It expands
the last accepted charge interval by the explicit current envelope over cache
age and reruns charge/SOC width checks. The estimate's accepted timestamp, epoch,
CAR baseline and expiry never move. Expiry is the earlier of last-acceptance plus
cache lifetime and seed time plus maximum epoch age. An excessive age, reversed
time, width or arithmetic failure returns labeled history. Read does not mutate
state. A failed observation already makes the cache historical immediately,
regardless of the remaining lifetime. These bounds are conditional on the
supplied physical envelope; no observed electrical measurement occurred during
a cached read.

Collector0074 integration contract
----------------------------------

The next kernel change can call this library from one in-driver gauge worker.
Use ``mt6357_gauge_collect_live()`` under its existing measurement locking; do
not read separate power-supply properties and call them a coherent bracket.
The mapping is concrete:

* ``live.generation/sequence/started_ns/finished_ns`` map directly after checked
  u64→signed64 conversion; ``live.error`` becomes observation.error.
* Endpoint ``i`` uses ``live.fg[i].current_ua.value`` and
  ``live.fg[i].charge_uah.value``. Pack successful CAR-low and CAR-high raw words
  as ``low | (high << 16)``. Preserve the separate original report for diagnosis.
* Voltage is checked ``live.adc[i][ISENSE].mv.value * 1000``; temperature is
  ``live.temperature_decic[i].value``, already using that bracket's current.
  Do not recompute NTC with a later current or silently use generic IIO fallback.
* PRESENT is ``live.fg[i].present.value`` only after its independent errno is
  zero. Disabled BATON is unavailable, not an absent pack. Check both ends.
* Endpoint error must include preparation, latch, release, every required raw
  FG/BATON word, converted current/CAR/PRESENT, all three ADC raw/scale/mV
  statuses and calculated temperature. ``live.error==0`` alone is not a license
  to ignore a newly added field error. Verify nested intervals/order lie inside
  the report before adapting; the pure observation intentionally carries only
  the complete bracket times, not a duplicate low-level report framework.

The collector's managed ADC supplier links and private in-driver lifetime still
apply. Add a stop/drain action before power-supply/resource teardown, serialize
worker/state/cache access separately from supply notifications, and never expose
a borrowed devres state pointer. A changed binding starts a new zeroed session;
fixed per-binding calibration is part of its provenance. Suspend/resume and
known engine/reset/removal events invalidate before further publication. There
is no hardware reset-generation field to map; do not manufacture one.

Before registering a normal battery supply, perform bounded initial sampling
and require a publishable, actual modeled estimate. Select policy operands with
explicit provenance/assumptions and review their tolerances; none are supplied
by this library. The worker then collects, adapts, reduces and updates a cache;
CAPACITY reads use the publication check and appropriate integer rounding.
Notify only meaningful validity/property transitions and keep last accepted
measurement time. If no valid estimate exists, return a real error, never a
constant percentage, false PRESENT or a shutdown override. Failure to obtain
initial data is a boot-readiness failure that must be resolved, not masked.

Actual Android integration needs more than an errno: BatteryMonitor currently
returns0 when CAPACITY fails before any good value, and after its 70-second
last-good expiry. BatteryService can then shut down a present, noncharging
battery with unsupported capacity level. Therefore initial acquisition before
normal publication, worker retry/availability and Health cache behavior must be
reviewed together. A failed seed with truthful ENODATA alone is not a successful
ROM boot solution. Physical model/policy validation and this kernel/Health
integration remain concrete prerequisites; this task makes the reducer usable
by that path and does not claim firmware beta readiness.

Independent evidence
--------------------

``tests/session-reference.py`` reads the frozen, hash-verified stock temperature
profiles and calls the independent Fraction physical-model reference. Its state
policy is a separately written Python reducer with unbounded rational interval,
time and SOC calculations, not a call into C and not a stock-daemon replay. It
checks both current signs, both counter signs, interpolated temperatures,
quantization, charge/discharge reversals, errors, history, event invalidation,
reseed causes, width limits and cached publication. Test policy numbers are
synthetic mathematical fixtures, not calibrated production thresholds.

``tests/session.c`` compiles the actual production sources under ASan/UBSan and
compares every estimate field, including retained history. Hand-derived stock
row fixtures cover exact charge/cutoff coordinates, valid modeled zero,
outward rounding, plateaus, programmer-error output preservation, positive and
nonnegative policy constraints, inclusive limits, partial endpoint errors,
special-code ambiguity and checked-overflow rejection. Normal runs also retain
the stock instruction and existing loaded-model suites. No hardware data or
closed stock daemon behavior is claimed for the new runtime acceptance policy.
