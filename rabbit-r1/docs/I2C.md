# MT6765 I2C bring-up

The I2C nodes now select an explicit MT6765 match, with its AP interrupt gate
and terminal error interrupts. Timing calculation still uses the inherited
MT8183 algorithm and needs electrical validation. All seven controllers specify
`clock-div = <1>`; only I2C5 is enabled on the r1 at present.

## Divider correction

Rabbit's [vendor driver](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/i2c/busses/i2c-mtk.c)
divides the main clock by the DT value when calculating timing. With
`set_dt_div` enabled, it also writes `(clock_div - 1)` to both fields of
`CLOCK_DIV`. The verified stock DT sets `clock-div = <5>` and `set_dt_div = [01]`;
the resulting register value is `0x0404`. This is a programmable divider, not
an extra fixed divide-by-five stage. The MT6765 clock provider supplies the
undivided `i2c_ck` through the `CLK_IFR_I2C_AP` gate.

Mainline's `mtk_i2c_set_speed()` treats the DT divider as a fixed hardware
prescaler. It independently chooses a programmable divider and
`mtk_i2c_init_hw()` writes only that chosen value to `CLOCK_DIV`. Keeping the
vendor DT value therefore divides the calculation by five without programming
that division into the controller. The upstream
[MT8183 nodes](https://code.googlesource.com/linux/torvalds/linux/+/c6e169bc146a76d5ccbf4d3825f705414352bd03/arch/arm64/boot/dts/mediatek/mt8183.dtsi)
also use a fixed divider of one with this driver.

At a 26 MHz input and a 100 kHz request, the original DT produces
`CLOCK_DIV=0`, `TIMING=0x17`, `LTIMING=0x1b`. Under the inherited MT8183 count
semantics, those registers describe 500 kHz. This is a calculated result from
the production code, not an oscilloscope measurement.

`scripts/test-i2c.py` compiles the production speed calculation, compatibility
data and hardware-initialization function with MMIO stubs. It decodes the
registers written by that function and checks the requested rate against the
undivided input clock. It covers all seven DT nodes, five input-clock fixtures,
and 100/400 kHz requests for both MT6765 and MT8183 compatibility data: 140 cases.
The test rejects the original divider. It does not exercise
DMA, interrupts, actual transfers or electrical setup/hold times.

## AP interrupt gate and transfer errors

In the stock version-2 transfer path, `mt_i2c_do_transfer()` writes
`I2C_MCU_INTR_EN = 1` to `V2_OFFSET_MCU_INTR = 0x40` before START. This is separate
from the event mask at `0x08`. The MT8183 fallback never programmed this AP
interrupt destination. Depending on inherited firmware state, enabling event
bits alone can leave a transfer without an interrupt delivered to Linux.

The new MT6765 match supplies this register and writes the gate before both a
normal START and the restart after a high-speed master code. Other supported
SoCs retain their existing start behavior. All MT6765 DT nodes use the single
`mediatek,mt6765-i2c` compatible; MT8183 is no longer advertised as a fallback.
The MT6765 register table also records the stock global `MULTI_DMA` offset
`0xf8c`, but no channel-routing operation is enabled by that table alone.

The vendor headers identify timeout (bit 5), DMA error (6), in-band interrupt
(7), and bus error (8), in addition to ACK/NACK, arbitration and completion.
The new interrupt enable mask follows the stock driver: timeout and bus error
are enabled; DMA error and in-band status are checked when reported alongside
an enabled event. All known status bits are cleared before starting a transfer,
and all event enables are cleared during initialization and when the wait ends.

The old handler only completed the wait on a transaction-complete or restart
interrupt. MT6765 fault IRQs now wake the waiter directly, including ACK/NACK
and arbitration loss. A fault cannot be discarded as the otherwise ignored
master-code restart. Results are:

| Condition | Result |
| --- | --- |
| Software or hardware timeout | `-ETIMEDOUT` |
| Arbitration lost | `-EAGAIN`, allowing the I2C core's bounded retry |
| DMA, bus or unexpected in-band interrupt | `-EIO` |
| ACK/NACK error | `-ENXIO` |

Before unmapping a failed transfer's DMA buffers, the driver now resets the
controller and DMA engine. Previously it unmapped and released the buffers
first, then reset on timeout/NACK. Failed transfers no longer ask the DMA-buffer
helper to copy read bounce buffers into the caller's buffer. This ordering also
applies to existing supported SoCs; their IRQ completion rules are unchanged.

`test-i2c-irq.py` executes the production transfer, IRQ, start, reset and error
callbacks with MMIO and DMA stubs under ASan/UBSan. It checks 36 MT6765
read/write/combined transfer cases, every terminal fault, two-stage repeated
starts, master-code restart handling, allocation/mapping failures, gate ordering
and MT8183 behavior. The DMA model deliberately keeps a failed transfer active
until reset and rejects premature unmapping. Mutations removing the gate or
terminal handling, restoring the old cleanup order, or copying failed buffers
are rejected. This proves software sequencing against the model, not interrupt
delivery, DMA quiescence or bus operation on hardware.

## Stock secure-firmware interface

The supplied RabbitOS v0.8.293 `tee.img` contains the ATF I2C service used by
the vendor resume callback. The image is 140944 bytes with SHA256
`e6de1331346ea1df4a0b78106de5ec5886f6eda99270de5e23ac9e7ba7101b62`.
`inspect-stock.py` extracts it from the checksummed official archive for analysis;
it is not patched or added to the flashing package.

The 64-bit service number is `0xc20002a0`; the 32-bit counterpart is
`0x820002a0`. The arguments are controller ID, register offset and value.
The selected dispatch arms at file offsets `0x5d08` and `0x5fc0` call the
same helper at `0xfcc0`. That helper indexes seven 12-byte records at
`0x1cc08` and uses their base-address and enable fields:

| Controller ID | Allowed base | Stock AP channel |
| --- | --- | --- |
| 0, 1, 5 | Rejected by the firmware service | No channel offset |
| 2 | `0x11009000` | `0x100` |
| 3 | `0x1100f000` | `0x100` |
| 4 | `0x11011000` | `0x100` |
| 6 | `0x1100d000` | `0x100` |

The IDs and offsets above agree with the verified merged stock DT. Earlier
notes omitted I2C6 from the channel list. The helper uses the table index,
not its first word: entry 3 actually contains 2 in that unused word.
Linux adapter numbers or aliases must not substitute for the firmware ID.

The helper takes the low 12 bits of the requested offset, permits `0xf00`
through `0xfa0`, and writes the low 16 bits of the value after a `dsb sy`.
It returns 0 after the write and -1 on rejection. The dispatcher sign-extends
that result into the saved caller context. Thus the vendor request
`(4, 0xf8c, 2)` writes a halfword to `0x11011f8c`. This establishes the
firmware-side write and return contract; it does not establish that the mode
became active on silicon or that nonsecure direct access is permitted.

`test-i2c-firmware.py` executes the unmodified selected dispatch arms, helper
and return path in Unicorn. Its 64 cases cover both SMC conventions at two
synthetic load addresses, all controller IDs, invalid IDs, register boundaries,
offset masking and halfword truncation. It checks exact memory writes, signed
results, stack restoration and callee-saved registers. The synthetic image
bias has low bits `0x3c0`, which resolves the helper's ADRP/ADD to the actual
table; it is not a claimed physical ATF load address. The real EL3 exception
entry, earlier caller/security checks, clock state and hardware are outside
this test. No host SMC is executed.

For the channel implementation, configure mode while the controller clocks are
enabled, check the firmware return value, and keep the adapter unavailable if
setup fails. Restore it before transfers after resume. I2C5 must not make this
call. Transfer setup still needs the AP bank; shared reset and arbitration
recovery must continue to use bank zero. The stock service alone does not
implement those Linux-side operations.

## Remaining controller work

The correction does not establish full MT6765/MT8183 compatibility. Rabbit's
driver has additional behavior that still needs review:

* `cnt_constraint` changes the step encoding when the sample count is one.
  The inherited algorithm also adjusts counts, but its divider-dependent rules need to be
  compared with the MT6765 hardware before claiming equivalent timing.
* Controllers 2, 3, 4 and 6 use additional channel offsets in the vendor DT.
  I2C4's AP transactions use `0x100`, while initialization uses channel zero.
  Its stock resume path asks ATF to restore shadow-register mode at `0xf8c`;
  the mainline driver still lacks that channel selection and firmware handshake.
  Channel FIFO clearing also uses bit 2, and the stock channel path masks direct
  ACK/NACK IRQs until completion. These differences are not fixed by the AP gate.
  The [touch audit](TOUCH.md) records the board evidence.
* Bus arbitration gates, reset behavior and 33-bit DMA need transfer tests.

The vendor source also contains a `DEBUGCTRL=0x28` write guarded by
`CONFIG_ARCH_MT6765`. Both the extracted stock configuration and the published
reference build select `CONFIG_MACH_MT6765` instead; `CONFIG_ARCH_MT6765` is not
defined. That inactive code is not evidence that the r1 needs this write, and
it has not been copied into mainline.

The r1 board enables I2C5 for the MT6370 power monitor at 100 kHz. It has no
vendor channel-offset requirement. Other I2C controllers remain disabled.
Successful real transfers, DMA completion and IRQ delivery are still unproven.

## r1 power devices

The verified stock overlay places the MT6370 sub-PMIC at address `0x34` on
I2C5, with SCL on GPIO48, SDA on GPIO49 and the PMIC interrupt on GPIO11.
The Type-C controller at `0x4e` uses a separate GPIO41 interrupt. These are r1
values; another MT6765 handset's interrupt assignments are not interchangeable.

The stock bus is **push-pull**, running at 3.4 MHz with the vendor `hs_only`
setting. The mainline board preserves push-pull mode and the GPIO48/49 function
muxes, but requests standard 100 kHz timing. It does not copy the vendor's
nonstandard high-speed transaction setting or overwrite the loader's pin bias.
The PMIC and Type-C interrupt definitions preserve the stock falling-edge type.

The MFD driver owns both I2C addresses, so there is no separate `0x4e` client
node. Its ADC is enabled; `iio-hwmon` exposes voltage/current channels and PMIC
junction temperature. The charger, Type-C, backlight and LED children are
explicitly disabled. Simply omitting them would allow the MFD core to create
devices without their board configuration. See [POWER.md](POWER.md) for the
initialization effects and the remaining charging work.

`test-r1-power.py` checks the compiled board against the extracted stock FDT:
controller registers/IRQ, bus mode, GPIO table muxes, I2C address and both PMIC
interrupt lines. It also checks the monitoring channels, disabled child gates
and stock backlight limits. CI runs it after extracting the verified firmware.

Before enabling the charger, map its ADC and interrupt dependencies, stock
current/voltage limits, Type-C detection and battery-temperature policy to
mainline. The vendor Android charging manager supplies policy that a generic
charger node alone does not replace. Display backlight support also depends on
this bus, so resolving its transfer path is a prerequisite for both features.
