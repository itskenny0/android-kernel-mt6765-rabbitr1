# MT6765 I2C bring-up

The fork's I2C nodes use the MT8183 driver fallback. The first confirmed problem
is a mismatch in the meaning of `clock-div`, copied from the vendor tree.
All seven controllers now specify `clock-div = <1>`.

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
`CLOCK_DIV=0`, `TIMING=0x17`, `LTIMING=0x1b`. Under the selected fallback's count
semantics, those registers describe 500 kHz. This is a calculated result from
the production code, not an oscilloscope measurement.

`scripts/test-i2c.py` compiles the production speed calculation, compatibility
data and hardware-initialization function with MMIO stubs. It decodes the
registers written by that function and checks the requested rate against the
undivided input clock. It covers all seven DT nodes, five input-clock fixtures,
and 100/400 kHz requests. The test rejects the original DT. It does not exercise
DMA, interrupts, actual transfers or electrical setup/hold times.

## Remaining controller work

The correction does not establish full MT6765/MT8183 compatibility. Rabbit's
driver has additional behavior that still needs review:

* `cnt_constraint` changes the step encoding when the sample count is one.
  The fallback also adjusts counts, but its divider-dependent rules need to be
  compared with the MT6765 hardware before claiming equivalent timing.
* Controllers 2, 3 and 4 use additional channel offsets in the vendor DT.
* Bus arbitration gates, reset behavior and 33-bit DMA need transfer tests.

The vendor source also contains a `DEBUGCTRL=0x28` write guarded by
`CONFIG_ARCH_MT6765`. Both the extracted stock configuration and the published
reference build select `CONFIG_MACH_MT6765` instead; `CONFIG_ARCH_MT6765` is not
defined. That inactive code is not evidence that the r1 needs this write, and
it has not been copied into mainline.

The r1 board still leaves its I2C controllers disabled while these differences
are investigated. No charger settings are changed by the divider correction.

## r1 power devices

The verified stock overlay places the MT6370 sub-PMIC at address `0x34` on
I2C5, with SCL on GPIO48, SDA on GPIO49 and the PMIC interrupt on GPIO11.
The Type-C controller at `0x4e` uses a separate GPIO41 interrupt. These are r1
values; another MT6765 handset's interrupt assignments are not interchangeable.

Before enabling the charger, map its ADC and interrupt dependencies, stock
current/voltage limits, Type-C detection and battery-temperature policy to
mainline. The vendor Android charging manager supplies policy that a generic
charger node alone does not replace. Display backlight support also depends on
this bus, so resolving its transfer path is a prerequisite for both features.
