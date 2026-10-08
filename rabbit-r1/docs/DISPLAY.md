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
instructions for 12 D-PHY rates, 24 video/pixel-format combinations and eight
lane/clock combinations. The actual r1 panel flags and native compatible
match table and DRM component lookup are included. Tests also cover the
fractional r1 rate, invalid modes, early power failures and the unchanged
MT8183 setup. The recorded
`0x64 = 0x1234` is a preservation-test seed, not a hardware value.

This audit covers selected setup writes, not all controller registers or
startup sequencing. It excludes the stock `MEM_CONTI` setup at `0x90`, packet
transmission, the analog PHY/PLL, clocks, MMSYS routing and complete DRM path.
The separate native PHY and display-path audits are described below.

An independent host API bug is fixed: successful writes now return `tx_len`
instead of zero, as required by `mipi_dsi_host_ops.transfer`. Without this,
a caller checking for short writes rejects every successful command.
`test-dsi-transfer.py` exercises the production callback with seven write packet
types, lengths 0..64 and all four host modes. It also checks mode restoration,
command/switch failures and an unchanged one-byte read path. The MMIO, IRQ and
transport operations are modeled, not hardware-tested.

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
Rabbit's default path additionally uses RSZ0 and places the overlays in a
different order. The test passes the chosen mainline path to the shipped routing
code, expanding its two virtual nodes between RDMA0 and COLOR0. It does not
claim to reproduce the stock default path or its scaling behavior.

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
| `0xf30` | `0x1` | `0x0` | CCORR0 input from COLOR0 |
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
It compares stock's four display mutex IDs and checks mainline add/remove for
all ten handles. MT8183 DSI/OVL/RDMA and MT2712's second MOD register are covered
as regressions. Nine deliberately broken route, mask, module and clock variants
are rejected. Traces are saved in `out/mt6765-display-path-audit.json`.

MMIO, module base addresses, logging and profiling are modeled. This does not
emulate CMDQ, hardware frame synchronization, clock waveforms or pixel flow.
It also does not establish that every route left active by LK has been shut
down: masked MOUT writes preserve unrelated outputs. Firmware path teardown,
OVL/RDMA configuration, IOMMU and complete startup sequencing remain to audit.

## Remaining acceptance work

1. Audit remaining host startup, clock parents, firmware path teardown,
   OVL/RDMA configuration and display IOMMU behavior.
2. Identify panel supply rails and implement cold power-on and power-off.
3. Enable the DSI graph and backlight together for a controlled hardware test.
4. Verify calibration handoff and PLL lock, measure link/frame timing, check an RGB test pattern and touch orientation,
   then exercise blank/unblank and repeated modesets with error logging.
5. Validate brightness, charging/thermal interaction and display recovery before
   treating the Android software-rendered UI as usable.

The existing MT6370 backlight corrections and limits are in [BACKLIGHT.md](BACKLIGHT.md).
