# rabbit r1 display bring-up

The kernel includes a board-specific `panel-rabbit-r1` driver and a disabled
DSI graph. The panel callbacks, selected native MT6765 host setup registers
and native PHY setup/shutdown sequence
match the shipped RabbitOS v0.8.293 code in offline tests.
**There is no working display result yet.** DSI, its PHY and
the MT6370 backlight stay disabled, and `CONFIG_DRM_MEDIATEK` stays unset.

## Panel evidence

Rabbit's published driver is named `ili9883_boe_mipi_hd`, but its active table
starts with the ST7701-style `ff 77 01 00 00 ...` bank selection. The ILI9883
sequence is commented out. Its `compare_id` callback accepts every returned ID;
that is not evidence of the fitted controller. The port therefore uses
`rabbit,r1-panel` instead of claiming an identified ILI9883 or ST7701 part.

The reference sources are pinned Rabbit kernel commit
`8167c8c1087f057d2ef302fc93b47554291687ec`:

- `drivers/misc/mediatek/lcm/ili9883_boe_mipi_hd/ili9883_boe_mipi_hd.c`
- `drivers/misc/mediatek/video/mt6765/dispsys/ddp_dsi.c`
- the extracted stock `merged.dtb` and shipped kernel `Image`

`test-r1-panel.py` verifies the shipped Image SHA256
`71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f`.
It executes the selected AArch64 panel callbacks and table interpreter with
Unicorn, modeling the reset, delay and DSI callbacks, ftrace and memset. The
recovered function offsets in that raw Image are:

| Routine | Offset |
| --- | --- |
| `lcm_get_params` | `0x6eed08` |
| `lcm_init` | `0x6eee04` |
| `lcm_suspend` | `0x6eee80` |
| `lcm_resume` | `0x6eeed8` |
| `lcm_suspend_power` | `0x6eef14` |
| `lcm_resume_power` | `0x6eef7c` |
| `push_table` | `0x6ef19c` |

These offsets are firmware-specific. The init table at `0x17808e8` contains
43 entries of 72 bytes: 39 commands, three delays and an end marker. Production
panel prepare/unprepare callbacks are compiled into a host harness with GPIO,
delay and DSI stubs. Their emitted operations are compared with the executed
stock callbacks, including payload lengths and the one-byte zero arguments
attached to sleep-out and display-on. Tests reject changed payloads, packet
families, reset polarity, ignored short writes and incorrect sleep-error cleanup.

The published `DSI_set_cmdq_V2` sends command bytes below `0xb0` as DCS packets
and the others as generic packets. The new driver preserves that selection.
The stock callback emulator stops at this transport boundary; it does not
emulate the packet builder, PHY, panel or physical bus.

The stock reset path selects GPIO45 in GPIO mode. Init drives it physically
high for 50 ms, low for 50 ms, then high for 150 ms. The table waits another
120 ms before its first command. The new active-low GPIO descriptor uses logical
values 0, 1, 0 to reproduce this waveform. It leaves the existing level alone at
probe and sets the output direction when DRM prepares the panel.

Failed initialization stops at the failed command, asserts reset, waits 120 ms
and returns the error. Short writes count as errors. Unprepare attempts both
sleep commands even after a failure, then asserts reset. After reset it returns
success so DRM clears its prepared state and a later prepare can initialize
again. Errors are logged. This cleanup is checked for both negative transport
errors and short transfers.

The enable callback also requires the panel core's prepared state. This prevents
the core from turning on the backlight after initialization failed.

No panel supply mapping has been established. The stock callbacks only drive
reset and send commands; they do not identify a regulator. The current driver
relies on firmware-established rails, as does the diagnostic configuration's
`regulator_ignore_unused`. It cannot cold-power the panel or implement complete
suspend power management. No guessed supply or dummy fixed regulator was added.
The unused physical-size constants in the vendor source are not copied.

## Timing and host work

The shipped `lcm_get_params` confirms these values:

| Setting | Value |
| --- | --- |
| Active image | 480 x 640 |
| Link | DSI0, two lanes, RGB888, sync-pulse video |
| Horizontal front / sync / back | 20 / 20 / 20 pixels |
| Vertical front / sync / back | 26 / 2 / 14 lines |
| Vendor PLL clock | 130 MHz |
| Active word count | 1440 bytes |
| Non-continuous clock | Enabled (`cont_clock = 0`) |
| Per-line clock LP | Disabled |

The vendor D-PHY code doubles `PLL_CLOCK`, requesting 260 Mbit/s per lane.
The provisional DRM mode requests 21667 kHz so the current host's
`pixelclock * 24 / 2` calculation requests 260.004 Mbit/s. This translation
preserves the requested link rate to kHz precision; it is **not a measured
refresh rate** or a proof that the host produces the vendor waveform.

The host now selects native data through `mediatek,mt6765-dsi`, also registered
in the DRM component table. Its video blanking counts exclude the stock packet
overhead and align to four bytes.
HSA/HBP/HFP are 52/52/48 for the r1. The previous MT8183 fallback produced
50/29/34. Burst takes precedence over the sync-pulse flag, as in the common
mode-selection code. RGB888 already had the correct hardware selector (3):
the vendor converts its software enum before writing it. The native path
corrects the two RGB666 selectors; MT8183 retains its existing values.

The native D-PHY digital timing calculation follows the stock defaults with
64-bit arithmetic in Hz. This avoids rounding a 260.004 Mbit/s request up to
261 Mbit/s before calculating timing. For the r1 mode, the selected registers
match the shipped setup:

| Register | Value |
| --- | --- |
| `DSI_TXRX_CTRL` (`0x18`) | `0x0001000c` |
| `DSI_PSCTRL` (`0x1c`) | `0x000305a0` |
| `DSI_MEM_CONTI` (`0x90`) | `0x0000003c` |
| `DSI_PHY_TIMECON0` (`0x110`) | `0x04040303` |
| `DSI_PHY_TIMECON1` (`0x114`) | `0x060f040c` |
| `DSI_PHY_TIMECON2` (`0x118`) | `0x040c0100` |
| `DSI_PHY_TIMECON3` (`0x11c`) | `0x00060902` |

The panel sets `MIPI_DSI_CLOCK_NON_CONTINUOUS` to reproduce the stock clock
lane flag. That flag is separate from the vendor's optional per-line clock-LP
mode, which the r1 does not request. The native host leaves `DSI_HSTX_CKL_WC`
(`0x64`) untouched, as the selected stock setup does. This remains a firmware
handoff dependency; its reset value and cold-start behavior are not established.

Mode validation rejects field overflow, packet-overhead underflow, invalid
lane counts and unsupported formats. Bring-up is restricted to the audited
125..1500 Mbit/s range covered by the digital timing audit;
these are not established MT6765 silicon limits. Power-on checks the full
64-bit rate before narrowing it and unwinds its reference count on format,
clock or PHY errors. A failed PHY startup stops further host programming.

`test-dsi-timing.py` executes these routines from the checksum-verified stock
Image, with MMIO, logging and profiling calls modeled:

| Routine | Raw Image offset |
| --- | --- |
| `DSI_Config_VDO_Timing` | `0x711a60` |
| `DSI_PS_Control` | `0x712a94` |
| `DSI_TXRX_Control` | `0x713024` |
| `DSI_PHY_TIMCONFIG` | `0x716540` |

Production callbacks are compiled with ASan/UBSan and matched against those
instructions for 12 D-PHY rates, 24 video/pixel-format combinations and 24
combinations of lane count, clock mode and initial memory-command value.
The actual r1 panel flags and native compatible
match table and DRM component lookup are included. Tests also cover the
fractional r1 rate, invalid modes, early power failures and the unchanged
MT8183 setup. The recorded
`0x64 = 0x1234` is a preservation-test seed, not a hardware value.

Native RX/TX setup explicitly programs `DSI_MEM_CONTI` with the DCS
write-memory-continue command (`0x3c`). Previously it inherited that field from
firmware or reset. The shipped `DSI_TXRX_Control` writes the same value twice;
mainline writes it once during lane initialization. Three synthetic initial
values check that the entire register is programmed, while the MT8183 path
leaves it untouched.

The test also executes the production lane-ready callback through power-on,
checks that a second power reference or already-ready call does not repeat
setup, and models lost controller state before another lane initialization.
The memory command must be configured before the modeled D-PHY reset and
lane initialization. The timing test stubs the reset and sleep controller;
this checks timing setup, not the physical startup waveform. The native sleep
controller is exercised separately below. Seven
compiled variants with missing/incorrect writes, a legacy write, stale state
or missing/late lane setup are rejected.

This audit covers selected setup writes, not all controller registers or
startup sequencing. It excludes packet transmission, the analog PHY/PLL,
clocks, MMSYS routing and complete DRM path.
The separate native PHY and display-path audits are described below.

Native interrupt acknowledgement now writes the complement of the handled
status snapshot to `DSI_INTSTA`. This register uses write-zero-to-clear
latches. The inherited read/modify/write could clear an event arriving between
its final read and write, losing a later command or video-mode completion.
The native handler preserves unhandled and newly latched bits. Other backends
retain their existing acknowledgement behavior.

`test-dsi-irq.py` executes the DSI0 branch of the shipped `disp_irq_handler`
at raw Image offset `0x742260`. Its acknowledgement at `0x742558` uses the
complement of the observed low 16 bits. In the stock CMDQ/ESD mode, bit 0
(`RD_RDY`) is excluded and left for the reader; CPU mode clears it. Mainline
handles CPU transfers and owns only `RD_RDY`, `CMD_DONE` and `VM_DONE` here
(`0x000b`), so it leaves the other status bits untouched. The test covers both
stock policies, BUSY set/clear, late events and the real callback dispatch
loop in 252 fixtures. IRQ-number/base lookup, logging, profiling and the final
registered callback are modeled. The fixture does not execute a panel read.

The production-handler harness checks all 65,536 low status combinations,
six new-event masks and two arrival points, including the final write window.
It verifies that preserved completions are delivered on the next invocation,
keeps existing software IRQ flags, and covers 192 MT8183 acknowledgement cases.
The old handler fails the late-VM_DONE case; seven compiled variants with
incorrect acknowledgement or backend selection are rejected. These are
ASan/UBSan tests with modeled W0C latches and wakeups. The native handler
returns after one status read even when BUSY stays asserted; the legacy
backend still uses its existing finite-delay model.
Repeated occurrences of the same event can coalesce in one status latch.

The native handler no longer acknowledges received data or polls BUSY in
hard IRQ context. The MT6765 transfer path waits for `RD_RDY`, copies all four
RX registers, asserts RACK, then waits for `CMD_DONE` and idle. A command-done
interrupt alone cannot complete a read. Both interrupt waits and idle polling
are bounded; an interrupted wait returns its error. Recovery stops and resets
the engine, clears stale completions, and restores the previous mode. A failed
read leaves the caller's buffer untouched. Other SoCs retain their hardware
handshake; the shared wait helper now propagates signal and timeout errors.

`test-dsi-command.py` executes the shipped CPU reader's successful RX-copy/RACK
slice at raw offsets `0x718634..0x7186c0` with 16 data/register fixtures. It
verifies four 32-bit RX reads before RACK and the copied stack data. This slice
starts after the stock read-ready wait and ends before command-done waiting;
it does not emulate the stock scheduler or prove packet reception.

The native host validates buffers, channel, short-packet lengths, supported
command types and queue capacity before touching hardware. It uses the DRM
packet constructor, including virtual-channel bits and explicit long-packet
types even for payloads of one or two bytes. The stock register header declares
128 command words and an eight-bit size field; one word holds the header,
leaving a maximum payload of 508 bytes. Larger writes fail with `-EMSGSIZE`.
This is a register-layout bound, not a measured hardware capacity. Reads decode
short/long responses by packet type, enforce the virtual channel, preserve the
stock ten-byte payload limit within the 16-byte RX window, and return protocol
errors for unknown or error responses. Each read queues a maximum-return-size
command before the BTA request, as the stock host does. Write acknowledgement requests and
non-command packet types currently return `-EOPNOTSUPP`.

Probe requests the native IRQ with `IRQF_NO_AUTOEN` before registering the host,
and initializes bridge metadata before any synchronous attachment can occur.
Power-on enables IRQ delivery only after clocks, lanes and stale-state cleanup
are ready. Final power-off masks the device interrupt and calls `disable_irq()`
before removing clocks. A mutex serializes native commands, output enable and
power references; software IRQ flags use atomic operations. A transfer while
off returns `-EHOSTDOWN` without MMIO. An idle command-mode controller skips the frame wait, and removal drops
outstanding power references. A busy controller must drain before lane sleep.

The ASan/UBSan harness compiles the production host dispatch, packet constructor,
command helpers, ISR, waits, probe, output and power callbacks. It checks
205,116 transactions/lifecycle cases: all channels and host modes, short writes,
long writes through 508 bytes, short and long reads (including one/two-byte long
responses), stale and unrelated completions, timeouts/signals, repeated power
references, failure recovery followed by another transfer, and probe ordering.
It models clocks, PHY, scheduling, IRQ synchronization and MMIO; it asserts no
MMIO with clocks off, IRQ masking before clock teardown, and no RACK in hard
IRQ context. Twenty-three compiled broken variants are rejected. The separate
legacy callback and timing tests still pass. These tests establish software
behavior under the models, not physical packet transmission or safe display
startup. The display graph remains disabled pending panel-power, firmware
handoff and hardware validation.

An independent host API bug is fixed: successful writes now return `tx_len`
instead of zero, as required by `mipi_dsi_host_ops.transfer`. Without this,
a caller checking for short writes rejects every successful command.
`test-dsi-transfer.py` exercises the production callback with seven write packet
types, lengths 0..64 and all four host modes. It also checks mode restoration,
command/switch failures and an unchanged one-byte read path. The MMIO, IRQ and
transport operations are modeled, not hardware-tested.

## Native pipeline power ownership

MT6765 acquires its DSI power reference during the DDP clock stage. This stage
returns errors to `mtk_crtc_ddp_hw_init()`, which unwinds earlier component
clocks, the mutex clock and runtime PM before connecting or starting the path.
The CRTC stays disabled and does not enable vblank after a failed acquisition.
Previously the void DDP start callback discarded DSI failures, after the path
was already connected and other components had started.

The DDP start callback now marks the successfully acquired native host as part
of the running pipeline. Bridge pre-enable requires this state before taking
its own reference. It cannot retry an incomplete CRTC startup independently.
Output enable requires both the running DDP state and the bridge reference.
Ownership changes are serialized by DRM commit callbacks; the existing native
mutex continues to protect hardware, transfers and the reference count.

Each owner releases only its own successful acquisition. DDP stop drops its
reference before upstream component clocks disappear. Clock-stage unwind also
releases that reference if a later component fails; the eventual clock-disable
callback is harmless after a normal stop. The bridge keeps DSI powered through
panel unprepare and releases it in post-disable. Duplicate acquisition/release
callbacks do not leak references or drop another owner's reference. Other SoCs
retain their start/stop power ordering; the new clock callbacks do nothing there.

`test-dsi-pipeline.py` compiles the actual CRTC clock loop, hardware-init and
atomic-enable functions, the DDP DSI descriptor and native DSI/bridge callbacks.
It exercises 180 failures followed by successful retries, placing DSI at each
of ten positions in a modeled component array. Failures cover the component
power domain, main runtime PM, mutex clock, native rate/clock/PHY/lane startup,
and every other component before and after DSI. It checks reverse clock unwind,
absence of path connection/start/vblank on failure, bridge gating, balanced
references, repeated callbacks and a panel command after DDP stop. Legacy
callback reference ordering is checked with an existing power reference.
Fifteen compiled faulty variants and the previous production callbacks are
rejected by the runtime checks.

Other display components, DRM services, clocks, PHY, MMIO and interrupt delivery
remain models. The event path is covered separately below. Panel rails, physical
page-flip behavior and firmware DMA handoff still need validation. The display
graph remains disabled.

## Events after failed display startup

The shared MediaTek CRTC callbacks now check whether hardware startup succeeded
before programming colors, updating display configuration, processing a frame
IRQ or enabling vblank. The ready flag is set before `drm_crtc_vblank_on()`,
which may immediately invoke the driver's vblank-enable callback. Normal shutdown
keeps the flag set while the core disables vblank, then clears it after hardware
teardown. An off-state vblank-disable callback does not access component registers.

DRM's runtime-PM commit helper enables outputs before committing planes. A failed
CRTC enable does not change the already accepted atomic state's `active` bit, so
plane begin/flush callbacks can still run. Begin now sends the event immediately
if the hardware is off or `drm_crtc_vblank_get()` fails. Only a successful get
creates a private pending event with a matching put. Flush skips color and
configuration writes while off. This avoids a stranded commit completion and
an unowned vblank-reference release; an immediate event is not evidence that
the requested framebuffer was displayed. A later explicit modeset can retry.

A new event cannot overwrite an older private event: the older one is completed
and its reference released under the event lock, with the condition logged.
The new event starts with a cleared pending-vblank flag, so the old flag cannot
complete it before flush. The lock order is configuration lock, then event lock.
Shutdown completes private events after hardware teardown, including when startup
failed and there is no hardware to disable. It also completes the inactive atomic
state's event, which previously depended on running hardware teardown. Active
state events belonging to a subsequent modeset stay with that new state.

`test-crtc-events.py` runs the production begin/flush, enable/disable, config,
IRQ and CMDQ callback code through the real DRM runtime-PM commit-tail ordering
and plane-commit loops. It also executes DRM's vblank enable/get/put helpers and
event-delivery helpers, including commit completion, fence signaling, timestamps
and user-event delivery. Core vblank on/off, hardware, other components, locks,
mailbox transport and scheduling are models. Vblank-enable error injection and
reference-call counters are instrumented in the extracted core helpers.

The ASan/UBSan builds cover 60 scenarios with CMDQ compiled out and 90 with it
compiled in: CPU, shadow-register and modeled command-queue paths; power-domain
and hardware-init errors; active state after failed startup; explicit retries;
vblank-reference errors; lost completions; shutdown; duplicate pending events;
three vblank-off policies; and both internal and userspace events. They check
single completion and fence release, balanced owned references, lock ordering,
no off-state register access, and delayed error callbacks after shutdown.
Eighteen compiled faulty variants and the previous callbacks fail in both builds.
The existing 180-case native DSI acquisition test still passes.

**Command-queue cancellation remains unresolved.** A shutdown timeout can leave
transport work outstanding. The event test explicitly supplies a delayed error
callback and proves only that it does not duplicate an event or access display
registers after shutdown. It does not prove that the GCE task was stopped. The
current mailbox flush can return an error while a task remains active, and its
error paths need auditing before packet reuse or display power removal is safe.
Full hardware synchronization and firmware handoff remain release gates.

### GCE flush ownership

The controller's flush path now balances its runtime-PM reference on every
return, including a failed resume and a running-thread timeout. A failed
suspend clears the suspend request and returns an error with all tasks still
owned by the controller. Cancelling a thread waiting for an event requires a
successful reset and disable before any cancellation callback. A reset failure
returns an error without releasing the tasks or their buffers. The existing
disable-register write still runs on reset failure; it is not treated as proof
that DMA has stopped. Clients must retain ownership until completion or a
successful cancellation. Running-thread polling uses the mailbox API's
millisecond timeout and preserves `-ETIMEDOUT`.

`test-cmdq-flush.py` executes the production flush, submission, task and IRQ
helpers together with the actual mailbox ring, submit, ACK and flush functions.
It also uses the kernel's atomic polling macro. MMIO/reset behavior, runtime PM,
locks, allocation and interrupt scheduling are models. Seventy-nine scenarios
cover one to three packets, native unshifted addresses, shifted addresses and
an address offset, callback-time buffer frees, failed stops and retries, PM
failures, completion during polling and timeout units. ASan/UBSan check memory
accesses. Thirteen faulty compiled variants and the previous production flush
fail these checks. The controller also compiles for AArch64.

The core probe demonstrates two remaining ownership hazards with the real
queue implementation. An allocation failure in `send_data` leaves the request
in the core ring even though `mbox_send_message` returns a nonnegative token.
With an empty controller task list, its flush can then report success while
that request remains queued. Conversely, the core calls `tx_tick` on a flush
error, and that call can submit the queued request. Neither a send token nor a
failed flush is a safe buffer-reuse boundary.

These changes cover controller flush only. The CRTC still ignores flush errors,
can reuse its packet without accounting for the core ring, and can remove
display power after an uncompleted task. Its error callback, normal IRQ task
retirement, channel shutdown and packet destruction ordering still need a
coherent ownership/recovery contract. The existing CRTC event tests model their
transport separately; they do not establish that contract. Display remains
disabled, and no physical DMA quiescence or frame presentation is claimed.

## Native host power sequencing

MT6765 does not use `CON_CTRL` bit 1 as a DSI-enable control. The published
`DSI_COM_CTRL_REG` marks it reserved, and the shipped power callbacks preserve
it while pulsing the engine and D-PHY reset bits. Native enable/disable helpers
now leave this bit untouched. MT8183 retains its existing enable control.
Native mode changes update only the two mode bits, preserving the other
`MODE_CTRL` fields as the shipped `DSI_SetMode` does.

The inherited lane helpers were unsuitable for native power management: their
sleep-entry path cleared the ULPM bits, and their wake path toggled individual
lane wake bits. The MT6765 path now follows the shipped controller sequences:

- Entry disables the high-speed clock, makes the other data lanes follow
  lane 0, requests clock-lane sleep, waits at least 1 microsecond, then requests
  data-lane sleep and waits for `SLEEPIN_DONE` (bit 15).
- Exit sets lane-follow and lane-count fields, selects sleep-out mode, programs
  the wake period, pulses `SLEEPOUT_START` (bit 2), and waits for
  `SLEEPOUT_DONE` (bit 6). It then clears the start and sleep-mode controls.
- The wake period is `floor(link_rate_Hz / 8192000) + 1`. Each unit is 1024
  byte-clock cycles, so the configured interval exceeds 1 ms. At the provisional
  r1 rate of 260.004 Mbit/s the field is 32. Other `TIME_CON0` fields survive.

These transitions run with clocks on, the power mutex held and CPU IRQ delivery
disabled. The relevant device completion source is temporarily enabled, its
status is polled with a two-second bound, and only that source/status is
cleared afterward. This intentionally replaces the stock IRQ/scheduler wait
while preserving its controller writes. A failed wake resets the engine,
cleans up sleep controls, leaves lanes unready and unwinds clocks, PHY and the
power reference. Failed shutdown sleep is logged and reset before clocks and
PHY are removed. Successful wake is followed by the stock engine reset; only
then are lanes marked ready and normal IRQ delivery enabled.

Before lane sleep or a temporary command transfer, the controller must become
idle. Native mode switching therefore tests `BUSY`, as the stock stop path
does, rather than treating a frame interrupt alone as sufficient. The wait
still wakes on interrupts and propagates signal/timeout errors. An already-idle
controller completes immediately. A shutdown drain failure resets the engine
before attempting lane sleep, including when the controller was already in
command mode.

`test-dsi-power.py` runs 240 complete shipped sleep/wake, engine-reset and
mode-setting calls against native register traces, covering dirty register
seeds, lane counts, link rates, immediate/delayed completions and timeouts.
The native path's additional write-zero-to-clear acknowledgements are checked
separately. Another 18 complete stock power-on/off executions include the real
sleep and reset routines and check clock-call order and preservation of reserved
bit 1. Analog PHY and clock service calls, delays, scheduler functions and
completion delivery are modeled; no physical lane state is measured.

The native harness uses the actual power, lane, mode, IRQ and transfer helpers.
It checks failed wake/shutdown, retries, repeated references, stale completion
status, IRQ/clock ownership, reserved and unrelated fields, and premature frame
interrupts while BUSY remains set. Fractional rates and exact counter boundaries
must satisfy the wake-period calculation. Twenty compiled broken variants are
rejected. The 205,116 transaction cases now also execute native lane sequencing;
legacy timing, transfer and IRQ checks pass. The AArch64 host object builds.

This does not establish a working display or a complete LK handoff. Panel
rails, firmware DMA quiescence, cold startup and physical sleep/wake behavior
still need validation before enabling the graph.

## Native PHY and PLL

`mediatek,mt6765-mipi-tx` now selects a native backend. It follows the published
`DSI_DPHY_clk_setting` and `DSI_DPHY_clk_switch` sequence, also recovered from
the shipped Image. The stock compiler inlines setup into `_DSI_PHY_clk_setting`
at raw offset `0x714ae4`; the on/off wrapper is at `0x715810`.

The shared PHY driver now provides optional setup before PLL enable and teardown
after PLL disable. Existing backends retain their original callback order.
The MT6765 setup holds an extra reference-clock reference across both phases,
so analog registers remain accessible before and after the PLL's own clock
reference. The 500 us bandgap wait runs in the sleepable setup callback;
the PLL enable callback uses atomic-safe register access and 2 us / 50 us delays.

On first power-on, the backend saves the ten calibration bits in each of the
five physical lane banks, as stock Linux does with LK's settings. It restores
that snapshot on subsequent power-ons, including zero codes. It also stops an
inherited active PLL and brings its lanes to LP00 before changing the analog
setup. Stock shutdown clears the analog clock, isolates and powers down the
PLL, sets the LP outputs, selects software lane control, and shuts down the
bandgap. The new backend preserves that order.

The PLL accepts 125..2500 Mbit/s with a 26 MHz reference, following the stock
divider thresholds. The host's narrower audited range remains in force.
At 260 Mbit/s, PCW is `0x50000000`; at the provisional DRM mode's 260.004 Mbit/s,
it is `0x500050a8`. The fractional value is calculated in Hz with 64-bit
arithmetic. Spread-spectrum modulation is disabled, as the shipped panel requests.
Invalid rates and reference frequencies are rejected. Clock failures unwind
the prepared analog state and reference count.

`test-mt6765-phy.py` compares the production backend and shared power callbacks
against the selected stock instructions. It checks 57 exact register-write
and minimum-delay traces: 19 rates across all divider boundaries, each with
three synthetic initial register patterns. Four repeated power-cycle fixtures
check saved calibration after modeled register loss. Separate tests cover the
fractional PCW, bad rates/reference clocks, clock failures, atomic context and
legacy callback order. Eight deliberately broken variants are rejected at runtime.

The reference emulator models DSI0 in normal display stage, D-PHY, no lane swap,
SSC disabled and the non-idle shutdown path. Calibration codes and MMIO state
are synthetic fixtures, not measurements from an r1. The clock framework,
MMIO and delay calls are modeled; neither PLL lock nor analog timing or signal
quality is verified. The delay comparison checks the requested minimum wait,
not wall-clock execution time.

This backend depends on firmware-established calibration, lane routing and
voltage settings surviving until its first power-on. MT6765 eFuse decoding,
cold-start supply programming, C-PHY, lane remapping and idle-only retention
are not implemented. The binding disallows calibration cells and drive-strength
overrides for this backend because it preserves firmware settings. The existing
display power-domain association remains a board-validation item.

## Display routing, mutex and RDMA clock

The DRM path shared with MT8183 is supported by the MT6765 routing tables:
`OVL0 -> OVL0_2L -> RDMA0 -> COLOR0 -> CCORR0 -> AAL0 -> GAMMA0 -> DITHER0 -> DSI0`.
Rabbit's Android kernel default path additionally uses RSZ0 and places the
overlays in a different order. The test passes the chosen mainline path to the
shipped Android routing code, expanding its two virtual nodes between RDMA0 and
COLOR0. It does not claim to reproduce that default path or its scaling behavior.
LK's primary route uses the same overlay order as mainline, as detailed below.

The inherited routing table omitted three selector writes and used one-bit
masks for two selectors with three inputs. The corrected route bypasses RSZ0,
feeds COLOR0 into CCORR0, and clears both selector bits before choosing input 1.
This prevents a previous selector value of 2 becoming invalid value 3.

| MMSYS offset | Mask | Selected value | Purpose |
| --- | --- | --- | --- |
| `0xf3c` | `0x2` | `0x2` | OVL0 to OVL0_2L |
| `0xf40` | `0x1` | `0x1` | OVL0_2L to RDMA0 |
| `0xf54` | `0x3` | `0x1` | RDMA0 input from OVL0_2L |
| `0xf48` | `0x1` | `0x0` | RDMA0 output bypasses RSZ0 |
| `0xf60` | `0x1` | `0x0` | RDMA virtual input bypasses RSZ0 |
| `0xf4c` | `0x3` | `0x1` | RDMA virtual output to COLOR0 |
| `0xf30` | `0x1` | `0x0` | Android driver's COLOR-route selector, retained |
| `0xf64` | `0x1` | `0x0` | LK's display selector: CCORR0 input from COLOR0 |
| `0xf50` | `0x1` | `0x1` | DITHER0 to DSI0 |
| `0xf68` | `0x1` | `0x1` | DSI0 input from DITHER0 |

MT6765 also requires DSI0's module bit 16 in the display mutex. Previously the
shared driver only selected DSI0 as the frame trigger. A native capability flag
now adds and removes its module bit as well; other platforms retain their
existing DSI behavior. The chosen path's MOD value is `0x1fb80`, with video
SOF/EOF value `0x41`. The register offsets are `0x30` and `0x2c`, respectively,
plus `0x20` per mutex ID.

The RDMA0 DT node now requests `CLK_MM_DISP_RDMA0`, MMSYS gate bit 10. Its old
`CLK_MM_MDP_RDMA0` reference selected bit 0, a different processing block.
The compiled DT clock provider, ID and gate are checked against stock wiring.

`test-mt6765-display-path.py` executes checksum-verified stock instructions:

| Routine | Raw Image offset |
| --- | --- |
| `ddp_path_init` | `0x72d22c` |
| `ddp_connect_path_l` | `0x72dae8` |
| `ddp_disconnect_path_l` | `0x72e400` |
| `ddp_mutex_set` | `0x7306d8` |
| `ddp_mutex_remove_module` | `0x72f7b0` |

The production mainline route table and shared connect/disconnect and mutex
callbacks run in an ASan/UBSan host harness. Four synthetic register states
exercise inherited selectors, unrelated-bit preservation, disconnect and
reconnect. Stock writes whole selectors and clears only MOUT on disconnect;
mainline uses masked updates and clears its selector fields on disconnect.
The comparison accounts for those differences and checks the selected fields.
It also runs `test-lk-display-path.py` and compares LK's primary route with
the production callbacks. The kernel programs both the Android driver's
`MDP_COLOR0_OUT_SEL_IN` at `0xf30` and LK's `DISP_COLOR0_OUT_SEL_IN` at `0xf64`.
Rabbit's register header defines both addresses. The previous implementation
left `0xf64` untouched, relying on firmware or reset state. The new write
explicitly selects COLOR0 in that field while retaining Android's setup.
The relationship between the two selectors still needs hardware confirmation;
the offline comparison establishes their software programming, not pixel flow.
It compares stock's four display mutex IDs and checks mainline add/remove for
all ten handles. MT8183 DSI/OVL/RDMA and MT2712's second MOD register are covered
as regressions. Nine deliberately broken route, mask, module and clock variants
are rejected. Traces are saved in `out/mt6765-display-path-audit.json`.

MMIO, module base addresses, logging and profiling are modeled. This does not
emulate CMDQ, hardware frame synchronization, clock waveforms or pixel flow.
It also does not establish that every route left active by LK has been shut
down: masked MOUT writes preserve unrelated outputs. Firmware path teardown,
IOMMU and complete startup sequencing remain to audit.

### LK routing evidence

`test-lk-display-path.py` executes Thumb instructions from the checksum-verified
stock `lk.img` (SHA256
`534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e`).
Its raw-file offsets include the 512-byte header; the address bias is
`0x47fffe00`. The emulator clears BSS at `0x480b7874..0x4816a594`, following
the bounds used by ARM startup at raw `0x2f8..0x30c`. The appended FDT overlaps
these runtime addresses in the file and must not be mistaken for initialized
display state.

| Routine | Raw LK offset |
| --- | --- |
| Route-register pointer initialization | `0x9ca8` |
| Scenario connect / disconnect | `0xa04c` / `0xa084` |
| Module-list connect / disconnect | `0x9808` / `0x964c` |
| Primary display initialization | `0x10a68` |
| Path initialization boundary | `0x8a00` |

The primary-init prefix selects scenario 0 and DSI0 for the modeled r1 DSI
panel. The route table is read from LK without replacing its contents:

`OVL0 -> OVL0_2L -> RDMA0 -> virtual0 -> virtual1 -> COLOR0 -> CCORR0 -> AAL0 -> GAMMA0 -> DITHER0 -> PWM0 -> DSI0`.

The two virtual nodes represent selectors, and PWM0 is not a pixel-processing
stage. Scenario 1 is the shorter RDMA-to-DSI path; scenario 2 routes the
overlays to WDMA0; scenario 3 connects both primary and WDMA paths. All four
scenarios run with four synthetic initial MMIO values, including disconnect
and reconnect. Returned stack and callee-saved registers are checked.
Traces and module names are saved in `out/lk-display-path-audit.json`.

The primary-prefix test models LCM discovery, path allocation, destination and
LCM-driver assignment, display-manager initialization and memset. It executes
the mode selection and stops before path initialization, clocks and module
callbacks. The separate route test then executes the stock scenario connector.
It does not emulate a complete LK boot or prove the register state at Linux
handoff. In particular, LK writes cached MOUT values as whole registers but
does not clear unrelated RSZ routes in these fixtures. Stopping inherited DMA,
reclaiming the firmware framebuffer and display IOMMU setup remain unresolved.

## SMI port translation

The native MT6765 SMI match now uses the per-port generation-2 configuration
callback. Previously it selected MT8167's callback, which writes a port bitmap
at LARB offset `0xfc0`. The shipped Rabbit kernel instead changes bit 0 of
`SMI_LARB_NON_SEC_CON`, at `0x380 + 4 * port`. With the old match, mainline
did not issue those translation-enable writes.

The native match has no foreign QoS table, direct-to-common bypass or secure
monitor flag. MT6765's existing single IOVA region starts at zero, so the
IOMMU callback supplies bank zero. The selected SMI callback therefore sets
only bit 0 of each requested port and preserves its other fields, matching
the shipped enable operation. Ports absent from the software mask remain
untouched. MT8167 and MT8173 retain their bitmap backends.

The four compiled LARB nodes use only their native compatible. The MT8192
fallback was outside the binding and would select another SoC's QoS table.
LARB2 also had a third clock name, `gals`, with only two clock specifiers;
the unmatched name is removed. The optional clock is now absent rather than
referring past the end of the clock list. Physical clock behavior is still
unverified.

`test-mt6765-smi.py` executes the shipped `m4u_config_port` function at raw
Image offset `0x8a13cc`. The actual port table at `0x179ddb0`, with 48-byte
entries, maps 52 ports across LARB0..3 (8, 11, 12 and 21 ports). The MMIO write
at `0x8a15e8` preserves all bits except translation enable. The test covers
enable and disable for every entry with three initial register values: 312
stock calls. All native enable writes are compared with those results.
The stock code takes the direct, nonsecure path; its table lookup, control
flow and register operations execute, while clocks, spinlocks, prefetch
invalidation, timers and logging/profiling are modeled. Firmware security
ownership is not established by this test.

The ASan/UBSan harness executes the production SMI bind/resume/suspend and
IOMMU configuration callbacks. It checks four arbiters, 37 masks (including
individual bits through bit 31), three dirty-register seeds, clock failure
before any MMIO, repeated resume after modeled state loss, software detach
and legacy bitmap writes. Bits outside the 52 populated stock ports are
software boundary tests, not claims about additional physical ports. The
compiled DT checks LARB ordering, native compatibles, clock counts and the
OVL0/OVL0_2L/RDMA0 IOMMU IDs. The LARB binding reports no diagnostics.
The old backend fails the register comparison; ten compiled faulty variants
are rejected. Six edited DT fixtures also reject foreign fallback, clock-list,
LARB ID/order and display-port regressions.

This change does not complete IOMMU bring-up. As in the shared generation-2
backend, removing a client from the software mask does not clear a previously
enabled hardware port. It is not a transition to physical addressing.
The controller audits below cover startup, address formats and software state
restoration. Inherited security/bank fields, active firmware DMA,
physical suspend retention and real mapped framebuffer access still require
validation. The display graph stays disabled.

## IOMMU controller setup

MT6765 write throttling uses bits 11:10 of `WR_LEN_CTRL` at `0x54`. The
shared driver previously cleared bits 5 and 21, leaving the native throttle
controls unchanged. Native initialization now uses the shipped mask and
programs coherence (`0x80 = 3`), write ordering (`0x84 = 0`), table walks
(`0x88 = 0`) and the idle-enable bit (`0x44`, clear bit 0). Other bits at
`0x44` and `0x54` are preserved.

The first attachment installs the page-table root before enabling table
walks, then performs a full TLB flush before configuring client ports.
Runtime resume restores the root and repeats the native controller setup
before the existing full flush. It does not enable walks for a bank without
an attached domain. Register snapshots now have an explicit validity flag:
zero is a valid saved write-length value and no longer suppresses restoration.
That snapshot correction also applies to the other MediaTek backends; their
register programming is otherwise unchanged.

`test-mt6765-iommu.py` executes the shipped `m4u_reg_init` at raw Image offset
`0x8a370c`, including its call to `m4u_invalid_tlb` at `0x89ed3c`. Eight dirty
register seeds and three protection addresses produce 24 initialization
traces. The stock LARB lookup and controller writes execute; DT lookup,
mapping, locks and logging are modeled. Native final values are compared
with those traces. The secure page-table root at `0x04` remains untouched,
and native fault interrupts remain limited to `0x3fff` instead of copying
the stock MAU monitoring mask. Synthetic high protection addresses exercise
the encoding through bit 33; they do not establish physical address limits.
The address-format audit below removes the unverified 35-bit table setting.

The ASan/UBSan harness extracts the production attachment, initialization,
full-flush and suspend/resume functions. Eight seeds across MT6765, MT8183
and MT6779 cover first attachment, repeated attachment, cold resume, three
state-loss cycles, zero-valued snapshots, allocation/clock/IRQ-request
failures and retry. It models page-table allocation, immediate runtime
suspend and IRQ registration, rather than actual scheduling or interrupts.
Fourteen compiled faulty variants fail the runtime checks; the native
AArch64 object also builds. CI runs the positive stock and lifecycle tests.

These checks do not execute DMA, hardware table walks, range invalidations
or IRQ delivery. IRQ masking and ownership across power transitions,
firmware DMA teardown, hardware table walks and mapped scanout remain
acceptance work. The display graph remains disabled.

## IOMMU address formats and faults

The native driver now selects the address format used by the shipped MT6765
code: 32-bit IOVAs, a 16 KiB root table and 34-bit mapped physical addresses.
The shared allocator keeps translation tables below 4 GiB. The previous
`IOVA_34_EN` and `PGTABLE_PA_35_EN` flags selected a 64 KiB root and allowed
table placement with unverified upper address bits. Those flags are removed
for MT6765. Other platforms retain their existing domain settings.
The IOMMU domain still reserves the top 8 MiB of its 32-bit IOVA range.

In the shipped Image, `m4u_pgtable_init` at `0x8ada68` requests 16 KiB and
checks 16 KiB alignment. `m4u_reg_init` writes the low 32 bits of the root
address directly. The 4 KiB, 64 KiB, 1 MiB and 16 MiB mapping functions at
`0x8accac`, `0x8ac86c`, `0x8ac568` and `0x8ac21c` encode PA bit 32 in
descriptor bit 9 and PA bit 33 in descriptor bit 4. They do not encode
PA bit 34. These observations define the software contract used here;
synthetic high addresses do not prove the board's physical address range.

`test-mt6765-address.py` executes three stock root-allocation fixtures and
48 stock mappings, covering all four page sizes, all four upper-PA values
and three address positions. The stock mapping fixtures use an existing
second-level table and disable the legacy 4G-remapping mode. Allocation,
locks, memset and logging are modeled; the mapping and alignment branches
execute. Stack, callee-saved registers and writes are checked. The native
harness compiles the production ARM v7s allocator, map, unmap and software
walk implementation together with the actual MediaTek domain finalizer.
Address, type, shareability, security and cache bits match the stock writes.
The generic format additionally sets nG and normal-memory TEX attributes;
those differences are recorded explicitly, not claimed to be tested on the
hardware. Cache coherency still requires a physical DMA test.

The same harness checks first/last bytes, duplicate mappings, address limits,
allocation failures, rejected high table addresses, domain sharing and
unchanged MT8183/MT6779 formats. The highest-IOVA case tests the raw page-table
format directly; real native mappings are restricted to the domain aperture.
The native map callback checks the complete batch before writing any entries.
The generic page-table callback previously checked only the first address,
so a multi-page request could cross the physical limit or wrap the IOVA index.
The new check uses division to avoid overflowing the byte count. Tests cover
physical and IOVA boundary crossings, oversized counts, empty requests and
valid batches ending at the physical limit, for all four page sizes.
Mapped addresses at or above 16 GiB are rejected instead of encoding an
unsupported third extension bit; the reserved IOVA gap is also enforced.

Native fault reporting now masks `FAULT_VA` to bits 31:12 after capturing
the write/layer flags. It reports `INVLD_PA` as a 32-bit value, matching the
stock handler; it cannot recover the upper bits of a faulting physical
address. The previous generic decoder could interpret reserved low bits as
IOVA or PA extensions and leave status bits in the reported IOVA.

`test-mt6765-fault.py` executes `MTK_M4U_isr` at `0x8a2d44` for 416 cases:
all 52 stock port IDs, both MMU slaves and four low-field patterns. The real
port-table lookup, address decode and interrupt-clear instructions execute.
Diagnostic port callbacks are disabled in the fixture; dump/query/profile
helpers and logging are modeled. All reports match the native handler.
The host harness checks all 4,096 low-field values for every port and slave,
plus accepted/rejected fault callbacks and missing domains: 425,990 cases.
It verifies clear-bit preservation and the existing full-flush sequence.
Physical IRQ delivery, power ownership, L2-only faults and simultaneous
fault servicing remain separate work.

Eighteen compiled regressions in address width, batch bounds, table placement,
descriptor bits, fault fields, ports, acknowledgement and flush behavior are rejected.
The native AArch64 IOMMU object builds. CI runs both positive tests. Neither
test establishes physical translation or permits enabling the display yet.

## Remaining acceptance work

1. Audit remaining host startup, clock parents, firmware path teardown,
   secure-engine handoff and display IOMMU behavior.
   The native RDMA/OVL setup and
   interrupt handling below still require hardware validation.
2. Identify panel supply rails and implement cold power-on and power-off.
3. Enable the DSI graph and backlight together for a controlled hardware test.
4. Verify calibration handoff and PLL lock, measure link/frame timing, check an RGB test pattern and touch orientation,
   then exercise blank/unblank and repeated modesets with error logging.
5. Validate brightness, charging/thermal interaction and display recovery before
   treating the Android software-rendered UI as usable.

The existing MT6370 backlight corrections and limits are in [BACKLIGHT.md](BACKLIGHT.md).

## Native RDMA0

`mediatek,mt6765-disp-rdma` now selects native platform data and a DRM component
entry. The MT8183 fallback has been removed. MT6765's unshared RDMA0 FIFO holds
384 sixteen-byte words (6 KiB); the fallback used 5 KiB and a 70% output-valid
threshold. Stock video mode uses a zero output-valid threshold. The native
setup uses the full unshared FIFO and explicitly selects no borrowed RSZ/WROT
SRAM. Arbitrary FIFO-size properties are rejected by both binding and probe.
The node is named `rdma@1400d000`: it is a display component, not a generic DMA
provider requiring `#dma-cells`.

Native clock enable stops the inherited engine, disables/clears its interrupts,
asserts reset and waits for it to leave idle, then deasserts reset and waits for
idle. Each phase is bounded to 100 ms. A failure releases reset and the clock
reference, prevents startup and logs the error. This follows `rdma_reset` in the
shipped Image at raw offset `0x7034a4`; the register state machine in the test is
modeled. No real reset completion has been observed.

The clock rate is sampled in the sleepable clock-enable callback. Configuration
can run from the CRTC interrupt callback, so it performs no clock queries or
sleeping operations. The sampled rate is used until clock disable. Dynamic
clock-rate changes while the display is active are not supported by this port.

Configuration establishes direct-link RGB input, clears the inherited matrix
selection, memory address/pitch and background, and writes the native 13-bit
width and 20-bit height fields. Memory-plane configuration selects memory
input afterward and recomputes its thresholds without overwriting them with
the shared driver's legacy `0x40402020` GMC value. A subsequent direct-link
configuration clears that memory state again. The CRTC's later `bpc = 0`
callback preserves the established output depth, defaulting to eight bits if
none has been established.

The native FIFO setup programs all 13 registers written by the active vendor
`rdma_set_ultra_l` implementation. The older implementation above it in the
published source is inside `#if 0`. The shipped routine is at raw Image offset
`0x7037bc`; the same Image checksum used by the panel tests is enforced.
The port retains its integer rounding and 25% consumption margin, uses the
requested refresh rate and sampled engine clock, and rejects arithmetic
overflow, insufficient fill rate or thresholds outside the unshared FIFO.
Invalid configuration stops the engine while its clock is held and prevents
startup. Mode parameters and clock rate are included in the error log.

For 480x640 RGB888 at the provisional mode's rounded 59 Hz, with a synthetic
230 MHz engine clock, the values are:

| Register offset | Value |
| --- | --- |
| GMC0 / GMC1 / GMC2 (`0x30`, `0x34`, `0x3c`) | `0x8023001e` / `0x801e0014` / `0xff` |
| FIFO (`0x40`) | `0x81800000` |
| SODI / DVFS (`0xa8`, `0xac`) | `0x00c7002d` / `0x001e001e` |
| DRAM (`0xc0`) | `0x00010000` |
| DVFS pre / ultra (`0xd0`, `0xd4`) | `0x002d0028` / `0x0028001e` |
| DRS leave / enter / urgent (`0xd8`, `0xdc`, `0xe8`) | `0x0012000f` each |
| SRAM selection (`0xb0`) | `0x0` |

`test-mt6765-rdma.py` compares 108 FIFO-write traces with the shipped
instructions, covering direct-link and memory video input, both vendor clock
states (230/457 MHz), six geometry/depth cases and zero/all-one inherited
register patterns. It additionally checks 30, 59 and 120 Hz requests. Stock
hardcodes 60 Hz in this calculation, so the non-60-Hz comparisons use a scaled
reference width with exactly the same pixel rate. That is an arithmetic test
fixture, not a stock panel mode or a measured clock. The reference dimensions
are recorded in `out/mt6765-rdma-audit.json`.

The production callbacks run under ASan/UBSan with clock, MMIO and CMDQ models.
Tests check reset transitions, both reset timeouts, clock failures, no startup
without valid configuration, invalid geometry/rates/depths, overflow, short
fill capacity, pending depth preservation, and memory-to-direct transitions.
Queued writes must leave MMIO unchanged until the modeled queue executes.
The MT8183 setup remains unchanged. Twelve deliberately broken variants are
rejected, including a clock query from IRQ context and leaked clock/reset state.
The native binding and compiled node pass validation; a FIFO override fixture
is rejected.

This work covers RDMA0 video operation with unshared SRAM. It does not implement
command-mode FIFO policy, shared-SRAM arbitration, display DVFS/SODI or secure
buffers. Programming the vendor's threshold
registers does not establish that those power-saving modes are safe to enable.
Hardware reset, FIFO occupancy, pixel output and underflow behavior remain
untested. The display graph and DRM driver remain disabled.

### Interrupts and register access

Native probe makes no register accesses and requests `IRQF_NO_AUTOEN`. The
CRTC enables the display power domain before component clocks. After RDMA's
clock enable and reset succeed, the driver enables its IRQ line with all
hardware sources still masked. Clock/reset failures leave the IRQ disabled.
Clock disable masks all sources and calls synchronous `disable_irq()` before
clearing pending status and dropping the clock reference. This also handles
CRTC cleanup if a later component fails before RDMA starts.

Start enables abnormal EOF and FIFO-underflow interrupts (`0x18`). The vblank
API independently controls frame-end (`0x4`), clearing an old frame-end flag
before enabling that source while preserving error flags. The r1 pipeline
normally uses OVL0 for CRTC vblank; RDMA must handle its errors even without a
vblank callback. Its handler reads status, acknowledges the observed bits with
`~status`, and dispatches vblank only for a frame-end whose source is enabled.
Different events arriving between the read and acknowledgement remain pending.
Repeated occurrences of the same latched bit can still coalesce.

Abnormal EOF and underflow have separate rate-limited error messages and
counters. Underflow includes input/output pixel and line counters at
`0xf0` through `0xfc`. Accounting continues when log output is rate-limited.
This provides diagnostics, not automatic display recovery. Existing platforms
retain their probe, clock and interrupt behavior.

`test-mt6765-rdma-irq.py` executes the shipped `disp_irq_handler` at raw Image
offset `0x742260`, including RDMA dispatch, event classification, acknowledgement
and counter updates. Its external logging/profiling, IRQ-number and module-base
helpers are modeled; the callback tables are empty. The 260 traces cover all
seven low status bits, reserved-bit fixtures and a different event arriving
during acknowledgement. Stock's `DDPERR` macro evaluates the abnormal counter
increment twice; the new driver deliberately counts the event once.

The production handlers run under ASan/UBSan for 1,560 combinations of status,
source mask, callback presence and late events. Tests also execute probe and
removal, eight probe failures, both reset timeouts, repeated clock/IRQ cycles,
CRTC error cleanup and a running IRQ completing during clock shutdown. The
MMIO model rejects access without a clock. Stock trace results are recorded in
`out/mt6765-rdma-irq-audit.json`. Fifteen deliberately broken variants are
rejected, including premature IRQ enable, lost status, incorrect vblank and
unsynchronized shutdown. These tests do not emulate the real GIC,
power-domain hardware or electrical behavior; those remain unverified.

## Native overlay engines

The four-layer `mediatek,mt6765-disp-ovl` and two-layer
`mediatek,mt6765-disp-ovl-2l` now have native data, bindings and DRM component
matches. The MT8192 fallbacks have been removed. Both retain the existing
eight-bit RGB/YUV format list and blending capabilities; neither advertises
AFBC or ten-bit input. Native plane formatting and blending are covered below;
pixel-output tests remain necessary.

Probe makes no MMIO accesses and requests `IRQF_NO_AUTOEN`. After the CRTC
powers the display domain, clock enable masks interrupts, stops the inherited
engine and pulses reset. It polls the flow-control register at `0x240` every
10 us for up to 20 ms. The shipped `ovl_reset` at Image offset `0x6f7854`
reads that register only once before its delay loop; the new code re-reads it.
A timeout drops the clock reference without enabling the IRQ or engine.

Successful reset disables all physical and constant-color layers, clears
extended-layer control and SBCH reuse state, and stops each physical layer's
RDMA. It clears random background mode and the upstream background input;
CRTC subsequently enables that input on the second overlay. This establishes
the intended background chain without inheriting firmware selections.
It does not disconnect leftover MMSYS MOUT routes or
quiesce an inherited RSZ engine; firmware-path teardown remains unfinished.

SBCH reuses transparent or constant regions based on layer-change tracking.
Mainline does not maintain that tracking, so native clock enable writes zero
to `OVL_SBCH` (`0x3a0`) and `OVL_SBCH_EXT` (`0x3a4`) after successful reset and
layer disable. The writes precede IRQ enable and engine startup on every power
cycle. Clock and reset failures do not attempt this cleanup. Other platforms
retain their existing behavior. `SBCH_CON` (`0x3a8`) is left alone; its
transparency-invalid status is not needed while the reuse controls are off.
No assumption is made that soft reset clears these registers.

The shipped Android `ovl_config_l` at raw Image offset `0x6fe054` clears both
registers when `DISP_OPT_OVL_SBCH` is off, or when the feature is on but
`pConfig->sbch_enable` is false. The test executes its post-layer branch from
`0x6fed38` to `0x6ffd28`. It covers both overlay modules, three initial register
patterns, both direct and queued paths, and all three disabled-feature/config
combinations: 36 fixtures. The per-frame-disabled branch also clears stock's
software tracking cache. Mainline has no corresponding cache to clear.

The test models module-address lookup, the option query, CMDQ writes, memset,
ftrace and logging; it does not execute layer layout, secure transitions or
the complete `ovl_config_l`. Its SBCH writes are compared with the production
native clock-enable callback under ASan/UBSan. The MMIO model retains dirty
SBCH state across reset and requires cleanup while the clock is on, the engine
is stopped and reset has completed. Repeated cycles, reset/clock failures,
untouched `0x3a8` and unchanged MT8192 behavior are checked. Eight compiled
variants with missing, incorrect or misplaced writes are rejected. Traces are
in `out/mt6765-sbch-audit.json`. Hardware reset and real region reuse remain
unverified.

Native configuration accepts nonzero dimensions up to the vendor's 4095 limit,
sets opaque-black ROI and constant-layer dimensions, and programs the shipped
real-time FIFO/request policy. It does not reset the engine from the atomic
configuration callback. An invalid mode stops the engine while its clock is
held and prevents startup.

| Register | Four-layer OVL0 | Two-layer OVL0_2L |
| --- | --- | --- |
| Per-layer GMC | `0x03ff03ff` | `0x03ff03ff` |
| Per-layer FIFO control | `0x00c00000` (192 words) | Same |
| Per-layer GMC2 | `0x203f007f` | Same |
| GREQ (`0x1f8`) | `0xf1ff7777` | `0xf1ff0077` |
| Urgent GREQ (`0x1fc`) | `0x7777` | `0x777` |
| Ultra source (`0x20c`) | `0x8040` | Same |
| Per-layer low/high buffer thresholds | `0` / `0x80000000` | Same |
| Functional DCM0/DCM1 | `0` / `0` | Same |

The two-layer urgent value deliberately follows the shipped code, including
its third request field. Layer enable preserves the native GMC value instead
of replacing it with the fallback's `0x01000100`. Native configuration clears
external-ultra blocking for the direct display path. Writeback/decoupled mode,
extended layers and dynamic bandwidth policy are not implemented.

Start establishes SMI IDs, rounding, GCLAST, clamping and the high-frequency
overlay clock bit before enabling scanout. Frame underflow, abnormal SOF and
per-layer abnormal EOF generate rate-limited logs with status, flow state and
an event count. Vblank independently controls frame completion. IRQ handling
uses write-zero-to-clear acknowledgement of the observed status and never
reports an error-only IRQ as vblank. Clock disable masks sources and waits for
running IRQ handlers before removing their clock. Error reporting does not
attempt automatic display recovery.

`test-mt6765-ovl.py` compares 18 native setup fixtures with the shipped
`ovl_ioctl` golden-settings path (`0x6fffe4`), ROI (`0x6f7908`), reset and start
(`0x6f71a4`). It compares final register values because the new code groups
writes by layer and configures startup fields before enabling the engine.
The interrupt mask deliberately follows the native vblank/error policy rather
than stock's frame-start reporting. Fixtures cover both blocks, three initial
register patterns, dimensions 1x1/480x640/4095x4095, and immediate/queued writes.

Another 76 shipped IRQ executions verify classification and acknowledgement.
The production handlers are checked across all 15 low status bits and callback
presence (65,536 combinations per block), masked vblank and late events. Tests
also cover six probe failures, clock/reset errors, repeated enable/disable,
cleanup before engine start, interrupt completion during shutdown and unchanged
MT8192 behavior. Compiled DT register ranges and interrupt specifiers match
stock. Seventeen deliberately broken variants are rejected by assertions or
comparison with the validated register traces. Results are recorded in
`out/mt6765-ovl-audit.json`.

MMIO side effects, reset latency, clocks, GIC/kernel services and CMDQ are models.
No real reset, memory fetch, blending, underflow recovery or pixel output has
been observed. Both the display graph and DRM driver remain disabled pending
the remaining integration and hardware acceptance work.

## RGB planes, opacity and reflection

The native MT6765 path converts [DRM's 16-bit plane opacity](https://docs.kernel.org/gpu/drm-kms.html#plane-composition-properties)
to the overlay's
eight-bit field using the upper byte. The previous low-byte mask mapped
`0x8000` (half opacity) to zero and made opacity wrap every 256 steps.
Native alpha blending stays enabled for RGB buffers without a pixel-alpha
channel too; otherwise their plane opacity was ignored in premultiplied mode.
Pixel-none blending and formats without pixel alpha select the unassociated
RGB format and constant blending. Alpha-bearing formats retain their selected
coverage or premultiplied format.

Horizontal reflection now starts at the last byte of the selected source row:
`cropped_addr + width * bytes_per_pixel - 1`. Using `pitch - 1` pointed into
row padding or pixels beyond the crop. Vertical reflection advances by
`(height - 1) * pitch`; rotate-180 composes with explicit reflections by XOR.
RGB configuration also clears inherited packed-YUV clipping and source-key
data, sets opaque-black layer color, and selects nonsecure input as stock does.
All changes are limited to native MT6765 data; the MT8192 behavior is retained.

`test-mt6765-ovl-plane.py` executes the shipped `ovl_layer_config` at raw Image
offset `0x700d90` and its format helpers at `0x744ea0..0x745064`. The emulator
models module base lookup, ftrace and disabled debug logging. It uses physical,
nonsecure memory layers without scaling, partial-update ROI or CMDQ. Vendor
format names describe memory byte order; they are mapped to DRM's little-endian
word formats explicitly. The static function's unused return value is not part
of the compiled ABI.

All 162 fixtures match the shipped per-plane register values: nine RGB formats,
three blend modes and six layers across both engines, with varied opacity,
crop, pitch and inherited register contents. Native CMDQ writes are also
checked through a queue model. The comparison allows native GMC programming
and enables the source layer last; it does not require stock write ordering.
Production callbacks run with ASan/UBSan.

Separate checks cover every opacity value for each format/blend pair
(1,769,472 combinations), 1,296 cropped rotation/reflection cases with padded
rows, disabling a plane without a framebuffer, and the unchanged MT8192 path.
The reflection oracle computes the selected rectangle's first fetched byte;
rotation was compiled out of the shipped r1 function, so this is **not** a
stock-instruction comparison for rotated buffers. Results are recorded in
`out/mt6765-ovl-plane-audit.json`.
Fifteen deliberately broken variants are rejected by the runtime assertions
or the stock-validated register traces, covering opacity, formats, reflection,
clipping, layer color, nonsecure state and constant blending.

No scanout, IOMMU access or resulting pixel colors are established by these
register tests. Addressing and packed-YUV crop work is described below.

## Framebuffer addresses and packed-YUV crops

The common pending-state helper now includes `fb->offsets[0]` in the DMA base.
Previously framebuffer creation accepted that offset and included it in its
allocation-size check, but the display driver fetched from the start of the GEM
object instead. Linear source-row arithmetic now uses `dma_addr_t`, avoiding
sign extension when a byte offset exceeds `INT_MAX`. The existing AFBC address
calculation also starts at the framebuffer offset; AFBC remains unadvertised on
MT6765 and its hardware decoding is not validated here.

Native UYVY/YUYV fetches begin on an even source pixel and cover whole two-pixel
chroma groups. An odd left edge moves the address back two bytes and sets
`CLIP.LEFT`; an odd right edge fetches one extra pixel and sets `CLIP.RIGHT`.
The source-size register contains the expanded width, while the destination
rectangle keeps its visible size. Every native plane update writes CLIP,
including zero for aligned YUV and RGB. The pending state carries the clipped
source x coordinate so asynchronous updates cannot reuse an old crop parity.

Both normal and asynchronous component checks run after DRM has clipped the
source and destination rectangles. Native OVL validation rejects fractional
source pixels, unsupported modifiers, zero or overflowing dimensions, pitches
beyond 16 bits, and destination edges beyond 4095. It accounts for chroma-pair
expansion before checking the row pitch, GEM allocation end and final 32-bit
DMA address. An odd framebuffer edge is accepted only when row padding and the
allocation contain the full pair. RGB width 4095 is valid; an expanded YUV fetch
of 4096 pixels is rejected. The existing prohibition on YUV rotation remains.

Async cursor checks now validate the new plane state instead of mutating the
old state's derived rectangles. Updates retain the new source/destination
rectangles and visibility, calculate the pending address before swapping
framebuffers, and disable a fully off-screen plane. Invisible updates skip
address calculation. DRM's normal scaling, framebuffer-coordinate and atomic
lifetime checks remain prerequisites; the native helper does not replace them.
The address and state fixes are shared by the MediaTek backends. The new
hardware bounds and YUV register handling apply only to MT6765.

`test-mt6765-plane-address.py` compiles the production pending-state, normal/async
check/update and OVL callbacks with ASan/UBSan. Its 144 packed-YUV fixtures match
the shipped `ovl_layer_config` writes for both formats, all six physical layers,
every left/right crop parity and three initial register patterns. Each fixture
uses a nonzero framebuffer offset and passes through the actual pending-address
helper; the stock fixture uses the equivalent adjusted buffer base. Immediate
and queued native writes are covered.

Additional checks cover 216 RGB crop/reflection combinations, offsets above
`INT_MAX`, AFBC header/body bookkeeping, 16/32-bit field limits, exact allocation
and DMA ends, pair expansion at odd framebuffer edges, disabled and invalid
states, error propagation, different old/new async framebuffers and unchanged
MT8192 layer checks. Twenty-three mutations are rejected by assertions or the
stock-validated traces. Results are in `out/mt6765-plane-address-audit.json`.

The test models DRM clipping and kernel services; it checks the driver's use of
those results, not the complete atomic core. It does not simulate DMA, IOMMU
translation, speculative hardware fetches or YUV-to-RGB pixel output.
Secure-engine handoff, physical bus behavior and display acceptance
remain unfinished. The graph and `CONFIG_DRM_MEDIATEK` remain disabled.
