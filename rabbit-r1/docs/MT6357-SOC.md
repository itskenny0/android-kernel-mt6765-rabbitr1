# Experimental live MT6357 capacity

`CONFIG_BATTERY_MT6357_R1_SOC` adds real measured V/I/T/CAR input to the existing
stock-profile model and reducer. It is **default off** and the shipping r1
configuration does not enable it. Its numerical assumptions are an uncalibrated
engineering experiment, not validated pack accuracy or a beta-readiness claim.
It does not use historical boot OCV, LK RAC, a fixed percentage, charging changes,
a battery-presence override, or an Android shutdown exception.

The module is now named `mt6357-battery.ko` because it links the gauge and model
objects. The driver/OF association remains `mt6357-gauge` /
`mediatek,mt6357-gauge`, and the power supply remains `mt6357-battery`.
The five files in `drivers/power/supply/mt6357-r1-model/` must be byte-identical to
`rabbit-r1/battery/{profile.c,profile.h,stock_profiles.c,session.c,session.h}`.
The host SOC suite fails on a mismatch. Edit the authoritative library, explicitly
copy it when preparing a patch, and retain all five sources in the kernel patch.

## Acquisition and publication

On the actual `rabbit,r1` board, this option requires all three linked IIO inputs,
the linked real charger, the verified 10000 uOhm shunt / 1000 permille gain,
16900 ohm NTC pull-up, 10000 uOhm thermistor series term and exact stock 21-point
NTC table. Missing or different inputs fail initialization. This identifies the
board configuration, not a replacement pack or its age/calibration.
Other boards keep the existing measurement-only driver.

Before power-supply registration, probe takes a real collector snapshot and runs
the reducer. It requires `session_read(now)` and a two-second future publication
margin to succeed. It retries at most eight times, 250 ms apart, with a six-second
decision deadline checked between calls. Regmap/IIO locks and bus operations can
block: neither that deadline nor the 500 ms sample acceptance limit forcibly
aborts I/O. Failure prevents registration and returns the measured/model error.
It must be handled as initialization failure by the coordinated Android startup
readiness mechanism, not converted into battery 0 or disguised as probe deferral.
The optional existing diagnostic capture reuses the seed report.

The collector latches CURRENT+CAR together before and after the ordered
voltage/NTC/reference/reference/NTC/voltage reads. The adapter checks every
transport, preparation, latch, release, conversion, detector and timing status;
raw/converted values alongside failures are not accepted. ADC voltage is integer
mV converted to uV. Current and raw signed CAR conversion retain their established
units. Temperature uses each bounding current and the in-window NTC/reference
reads. The full original report is retained separately from the compact reducer
observation. Independently valid absence or disabled FG operation invalidates the
session even if another field failed.

One worker acquires a new report nominally every two seconds. CAPACITY performs
no hardware I/O: it reads the reducer under a separate mutex, checking the actual
boottime and widening uncertainty for cache age. Failure returns an errno and
leaves the output untouched. An old value is never marked fresh after a failed
observation. Validity/integer-capacity transitions notify the power-supply core;
stable repeated observations do not create notification loops. The worker compares
old-cache validity after I/O, so expiry during a valid capture still notifies a
same-percent fresh recovery. Reads do not send notifications themselves: there is
no separate timer-bounded expiry uevent if the worker or hardware is stalled.

Integer CAPACITY uses `(soc_basis_points + 50) / 100` after checking 0..10000.
This is uniform nearest-percent rounding, ties upward: 0, 1 and 49 bp become 0%;
50 bp becomes 1%; 9949 bp becomes 99%; 9950..10000 bp become 100%. There is no
positive floor, special startup percentage or forced FULL state. Genuine modeled
zero remains zero. Charger STATUS remains its actual independently read value.

## Lifetime and power transitions

The worker releases the SOC mutex before collector I/O, which uses the existing
measurement mutex and provider links. CAPACITY readers therefore do not wait for
an entered ADC transaction. Before collection the worker snapshots a software
event token; suspend/stop changes the token, invalidates the reducer and drains
entered work without holding the state mutex. A report crossing that event is
discarded. The reducer also requires capture start strictly after a dated event
barrier. Resume queues a fresh collection after one jiffy and keeps CAPACITY
unavailable until it succeeds. Suspend gaps are never trusted as CAR continuity.

The SOC stop action is installed after supply registration and the existing
STATUS stop action. Devres therefore drains SOC first, then unregisters the
blocking charger notifier and drains STATUS, then unregisters the supply, then
releases locks/provider references. Shutdown stops both workers; the existing
STATUS devres action still owns notifier unregister. The same links protect
providers during acquisition. No exported borrowed-state getter is added.

Binding generation is only a software lifetime. There is no hardware reset epoch
in the collector. Small hidden CAR resets and battery removal/reinsertion between
BATON reads can be unobserved; normal-looking endpoints cannot prove otherwise.
The reducer conservatively loses continuity on observed CAR special/sign changes
and impossible deltas. It does not invent modulo unwrap. A fresh seed freezes its
whole-degree profile/policy for the epoch; it never round-trips charge through an
OCV plateau. Known removal/reset/suspend events require a genuinely later capture.

## Explicit experimental assumptions

| Input | Value | Basis and limitation |
|---|---:|---|
| Reference cutoff | 33500 ×0.1 mV | Stock battery0 profile policy input; not new charger settings |
| Reference discharge | 5000 ×0.1 mA | 500 mA stock-reference load, independent of signed live I |
| Shunt / meter / DC ratio | 100 / 75 ×0.1 mOhm / 100% | Verified stock configuration; profile resistance is modeled, not LK measured RAC |
| Observed current ceiling | 3000000 uA | Stock 3 A tracking heuristic, not a precision specification |
| Whole-window current envelope | 10299600 uA | Verified normal decoder full scale; explicitly assumed over unsampled time |
| Voltage / current / temperature motion | 30000 uV / 300000 uA / 10 deciC | Warm stock comparison scales / engineering temperature limit; endpoints cannot exclude a transient |
| Model-root / cutoff error | 30300 / 30300 uAh | Assumed 3% of 1010000 uAh profile span; not derived sensor-error coverage |
| CAR delta error | 5250 uAh | 100 uAh conversion allowance plus assumed 3% full-scale drift over 60 s, rounded outward |
| Maximum q / SOC interval width | 101000 uAh / 2000 bp | Explicit experiment limits; no availability-driven cold widening |
| Capture / inter-sample / epoch limits | 0.5 / 10 / 60 s | Engineering freshness limits |
| Cache validity / worker interval | 5 / 2 s | Cache can fail earlier from uncertainty width; no timer is a hard scheduling guarantee |

Profile temperature is restricted to -10..50 C before interpolation; cold,
ambiguous, out-of-curve, first-cutoff-missing or uncertainty failures remain
unavailable. A PROFILE_END result is not a usable cutoff. The error budget is an
external model assumption: padding one root does not prove that V/I/T uncertainty
cannot introduce extra roots. Hardware comparison across load, temperature,
charge/discharge and aging is still needed to validate or replace these budgets.

## Tests and integration status

`test-mt6357-soc.py` compiles the actual complete gauge body and exact portable
sources under ASan/UBSan with SOC on and diagnostics off/on. It exercises raw ADC
through real NTC conversion and seed, immediate property callbacks at registration,
all adapter field errors and timing failures, retry/deadline behavior, cache-only
reads/expiry, worker transitions, CAR discontinuity, epoch age, PM/shutdown,
probe/resource failures, unsupported inputs, and a worker paused inside IIO while
suspend/removal must wait. The hardware mock permits only existing FG latch/release
writes. No PMIC reset, active load, charging write or real hardware execution occurs.

A separate exact Fraction calculation uses the captured stock 25 C profile to
check all roots/cutoff/intervals/percentage outputs of the raw-voltage fixture
without running the C model. Representative modeled results include 29 bp→0%,
58 bp→1%, and 9991 bp→100%; out-of-curve fixtures remain rejected. This is a model
oracle, not stock daemon instruction equivalence or physical accuracy.
Existing current/NTC, counter, STATUS and collector assertions remain active in
their affected harnesses with the new configuration disabled.

The private ARM64 check links the real MODULE composite object using GCC13.3,
W=1, -Werror and -fstack-usage. It is not a complete .ko modpost, full kernel,
or hardware test. Largest individual frame is the existing pure candidate1952 B;
step960, read224, collector768 and worker80 B. This is not a complete kernel
call-chain or interrupt-stack bound. Coordinated Android same-monitor priming
and readiness gating remain required before any experimental ROM installation.

The integrated code also passed an incremental full ARM64 kernel, modules and
Image link with SOC, diagnostics and PowerVR enabled. All ten modules and the
kernel are AArch64; the kernel has no undefined symbols. This build emitted no
warnings or errors and reused objects from the earlier full kernel build.
`tests/battery/soc-kernel-link.json` records the source fingerprint, config,
artifact hashes and scope. The gauge was built-in for this check; a modular
gauge `.ko` modpost remains separate. The shipping config, diagnostic package
and Android kernel were not replaced.
