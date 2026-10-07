# MT6370 backlight bring-up

The backlight driver is compiled into the r1 kernel. Its board node remains
disabled while panel power, reset and display scanout are being integrated.
There has been no brightness or protection test on an r1.

## Register corrections

The inherited driver's property handling could set selected register bits but
could not clear them: it passed the requested value as the `regmap_update_bits()`
mask. For example, selecting the lowest OVP threshold from a previously higher
setting left the old threshold in place. Clearing the PWM-enable property also
left an inherited PWM-enable bit set, making brightness depend on a signal Linux
was not managing.

The driver now masks the full fields it owns. Boolean properties set or clear
their fields. Numeric properties update their complete fields when supplied;
omitted thresholds and hysteresis values retain their inherited settings.
Unrelated PWM sampling/deglitch, switching-frequency and reserved bits survive.

Rabbit's [vendor header](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/misc/mediatek/pmic/mt6370/inc/mt6370_pmu_bled.h)
defines the brightness mapping at `BL_EN[1]`: one selects linear mapping and
zero selects exponential. The [vendor initialization](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/misc/mediatek/pmic/mt6370/mt6370_pmu_bled.c)
and shipped AArch64 instructions agree. Mainline instead defined bit 0 and
omitted the mapping bit from its final mask. The correction sets bit 1 for linear
mode, clears it for exponential mode, and preserves bit 0, the PWM polarity.

Richtek's RT5081 datasheet, DS5081-00, pages 19 and 103–105, also documents these
fields. The RT5081 is handled by the same vendor backlight path and mainline
common variant. The manufacturer's original PDF URL currently returns 404;
a [copy of the manufacturer's document](https://datasheet4u.com/pdf-down/R/T/5/RT5081-Richtek.pdf)
and its [extracted text](https://manuals.plus/m/49607a49ed1974852cdc32a19c8b855b5943a6fd9145a29f8062d72ca7796fac)
were inspected. This corroborates the register interpretation; it does not
identify the silicon fitted to a particular r1.

The OVP and OCP shutdown controls are active-low at `BL_BSTCTRL[7]` and `[3]`.
Setting them selects fault reporting without shutdown. The two `*-shutdown`
properties now clear the relevant disable bits when present. Absent properties
select reporting only, consistent with the stock initialization. The numeric
OVP/OCP fields control boost protection thresholds, not battery-charge limits.

The vendor explicitly forces both disable bits on chip revisions 0 and 1.
Mainline now retains the revision from `DEV_INFO[3:0]` and rejects a request to
enable shutdown on those revisions with `-EOPNOTSUPP`, before changing backlight
registers. It does not silently advertise a requested protection mode that it
does not support. This driver restriction follows the vendor's revision rule;
it is not a claim that a published silicon erratum has been found. The physical
effectiveness of either fault mode is untested.

Brightness updates now preserve the unused bits of `BL_DIM2`, then write
`BL_DIM1` to latch the value. Common parts retain their 11-bit encoding and
MT6372 parts retain their 14-bit encoding. Linux brightness zero disables the
backlight; positive levels encode `brightness - 1`. Invalid or missing channel
selection is rejected before register configuration.

## Stock r1 configuration

The checksummed stock DT supplies:

| Setting | Value |
| --- | --- |
| LED channels | All four, mask `0xf` |
| Mapping | Linear |
| OVP selector | 3, corresponding to 29 V |
| OCP selector | 2, corresponding to 1.5 A boost current |
| External PWM | Enabled |
| PWM sample / deglitch / hysteresis | Selectors 2 / 1 / 0, hysteresis enabled |
| Brightness limit | Vendor value 512 |
| Ramp / flash ramp | Selectors 3 / 1 |
| Current scaling / low-pass coefficient | Selectors 0 / 0 |

With the MT6370 vendor-ID fixture, the selected stock setup writes
`7e ed b4 30 07 3f 00 08 8c 80 ff 00` to registers `0xa0..0xab`, then zero to
`0xad`. The initial brightness code is 511. The board node's Linux limit of 512
encodes the same upper code; its default brightness is zero. Its protection
thresholds match the stock values. It does not request fault shutdown.

The stock setup uses external PWM. A future I2C-only brightness path must
explicitly disable that input, while a PWM path must also establish its pin mux,
clock and display-PWM state. The new mask handling makes that choice effective;
it does not prove either path produces light. Panel supplies, reset sequencing
and scanout must be integrated before enabling the board node.

## Offline checks

`test-mt6370-backlight.py` compiles the production property, chip-identification,
brightness and readback callbacks with property/regmap/GPIO stubs under
ASan/UBSan. It checks 65,536 property/register cases across all initial byte
values, both mapping modes, all numeric selectors, external enable, PWM,
hysteresis and fault flags. Additional cases cover omitted settings, clamping,
all recognized vendor IDs/revisions, invalid channels, register failures and
every brightness code in both formats. It checks register order, untouched bits,
readback and error propagation. Seven mutations restore the old masks, wrong
mode bit, missing mode mask, reversed shutdown polarity, absent revision gate or
unmasked brightness write; all fail runtime assertions.

`test-stock-backlight.py` executes the shipped kernel's selected setup fragment
at file offsets `0x8b13c0..0x8b15d0`, with the stock Image SHA256 checked first.
It supplies parsed platform-data fixtures and models only the two PMIC write
calls. The 48 cases cover six vendor IDs and mapping/external-enable/PWM choices.
Four extra cases seed a synthetic initialization byte to expose the revision
branch; the unmodified stock array already has the disable bits set. Each case
checks the exact requested register payload and bounds every memory write.
The stock DT values are checked independently. This is not a full probe, regmap,
I2C, brightness or fault-injection test on hardware.

Both tests run in CI. The AArch64 object and full kernel builds check integration
with the real interfaces. The next acceptance evidence is identification of the
fitted PMIC, successful register transfers/readback, the display power sequence,
measured brightness changes and controlled protection testing.
