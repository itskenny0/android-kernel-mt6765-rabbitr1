# rabbit r1 display bring-up

The kernel includes a board-specific `panel-rabbit-r1` driver and a disabled
DSI graph. The driver reproduces the shipped RabbitOS v0.8.293 panel callbacks
in offline tests. **There is no working display result yet.** DSI, its PHY and
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
| Per-line clock LP | Disabled |

The vendor D-PHY code doubles `PLL_CLOCK`, requesting 260 Mbit/s per lane.
The provisional DRM mode requests 21667 kHz so the current host's
`pixelclock * 24 / 2` calculation requests 260.004 Mbit/s. This translation
preserves the requested link rate to kHz precision; it is **not a measured
refresh rate** or a proof that the host produces the vendor waveform.

The inherited MT8183 DSI fallback differs materially from stock MT6765:
`DSI_Config_VDO_Timing` aligns HSA/HBP/HFP byte counts to four bytes, yielding
52/52/48 for this panel. The mainline fallback subtracts PHY transition cycles
from HBP/HFP and yields 50/29/34 with the provisional mode. The native timing,
PHY register setup, clocks, MMSYS routing and complete DRM path need an audit
before enabling the graph. The panel sequence alone cannot resolve those gaps.

An independent host API bug is fixed: successful writes now return `tx_len`
instead of zero, as required by `mipi_dsi_host_ops.transfer`. Without this,
a caller checking for short writes rejects every successful command.
`test-dsi-transfer.py` exercises the production callback with seven write packet
types, lengths 0..64 and all four host modes. It also checks mode restoration,
command/switch failures and an unchanged one-byte read path. The MMIO, IRQ and
transport operations are modeled, not hardware-tested.

## Remaining acceptance work

1. Resolve the native MT6765 host/PHY timing and display routing differences.
2. Identify panel supply rails and implement cold power-on and power-off.
3. Enable the DSI graph and backlight together for a controlled hardware test.
4. Measure link/frame timing, check an RGB test pattern and touch orientation,
   then exercise blank/unblank and repeated modesets with error logging.
5. Validate brightness, charging/thermal interaction and display recovery before
   treating the Android software-rendered UI as usable.

The existing MT6370 backlight corrections and limits are in [BACKLIGHT.md](BACKLIGHT.md).
