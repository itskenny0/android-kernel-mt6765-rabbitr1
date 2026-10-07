# rabbit r1 display bring-up

The kernel includes a board-specific `panel-rabbit-r1` driver and a disabled
DSI graph. The panel callbacks and selected native MT6765 host setup registers
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
125..1500 Mbit/s range, within the inherited PHY driver's software limits;
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
The PHY still inherits an MT8183 fallback and must be audited before activation.

An independent host API bug is fixed: successful writes now return `tx_len`
instead of zero, as required by `mipi_dsi_host_ops.transfer`. Without this,
a caller checking for short writes rejects every successful command.
`test-dsi-transfer.py` exercises the production callback with seven write packet
types, lengths 0..64 and all four host modes. It also checks mode restoration,
command/switch failures and an unchanged one-byte read path. The MMIO, IRQ and
transport operations are modeled, not hardware-tested.

## Remaining acceptance work

1. Audit the remaining host startup, analog PHY/PLL, clocks and display routing.
2. Identify panel supply rails and implement cold power-on and power-off.
3. Enable the DSI graph and backlight together for a controlled hardware test.
4. Measure link/frame timing, check an RGB test pattern and touch orientation,
   then exercise blank/unblank and repeated modesets with error logging.
5. Validate brightness, charging/thermal interaction and display recovery before
   treating the Android software-rendered UI as usable.

The existing MT6370 backlight corrections and limits are in [BACKLIGHT.md](BACKLIGHT.md).
