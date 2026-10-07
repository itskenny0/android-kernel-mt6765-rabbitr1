# MT6765 I2C bring-up

The r1 enables I2C4 for experimental touch probing and I2C5 for power
monitoring at 100 kHz. The MT6765 match now supplies its own timing, divider,
timeout, AP interrupt gate and channel setup. Neither bus has been tested on
hardware.

## Native counter and timeout programming

The driver follows Rabbit's [published version-2 implementation](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/blob/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/i2c/busses/i2c-mtk.c),
checked against the actual instructions in the supplied stock kernel.
`clock-div` now means the programmable divider for MT6765, with a valid range
of 1 through 8. All seven SoC nodes use the stock value of 5. Both fields of
`CLOCK_DIV` receive 4 (`0x0404`), and timing uses the parent clock divided by 5.
This interpretation is specific to MT6765; older matches keep their fixed
prescaler semantics and existing timing calculations.

The former MT8183 fallback divided its timing input by the DT value without
programming that division into hardware. The earlier correction used a fixed
prescaler of 1 with that fallback. Restoring the stock DT value here is paired
with explicit hardware programming; retaining that earlier calculation would
reintroduce the mismatch.

MT6765 retains the raw step count when the sample divider is one. For larger
sample dividers it encodes both counts minus one. The driver follows the stock
50/50 standard-mode and 45/55 fast-mode targets, kHz rounding, and separate
master-code/high-speed fields. It avoids a raw 64-step count with sample=1,
which the vendor code masks to zero, by selecting sample=2 and step=32 instead.
High-speed fixtures use the standard master-code phase; the vendor's special
`hs_only` mode is not exposed. The board remains at 100 kHz.

Every transfer refreshes the calculation from the enabled parent clock and
programs the selected bank. `HW_TIMEOUT` at offset `0x4c` gets the stock
2 ms count derived from `LTIMING`, and `TIMING` gets the vendor's bit-0 timeout
enable. Fast/high-speed `EXT_CONF` configuration, clock extension and transaction
delay follow the same source. The unverified MT8183 writes to `SDA_TIMING` and
`SCL_MIS_COMP_POINT` are no longer made for MT6765. Invalid timing fails before
starting a transfer, and probe now checks the timing result too.

At a synthetic 26 MHz parent, divider 5 and 100 kHz request, the programmed
values are `TIMING=0x1b`, `LTIMING=0x1a`, `CLOCK_DIV=0x0404` and
`HW_TIMEOUT=386`. These are register values, not measured waveforms or proof
that the selected timeout lasts 2 ms on hardware.

`test-i2c.py` compiles the production timing and initialization functions with
MMIO stubs: 70 MT8183 fixtures and 140 MT6765 bank/setup fixtures, including all
seven DT dividers. It rejects invalid inputs and checks the 64-step boundary.
The transfer harness checks clock changes between transfers and rejection of
an invalid parent without MMIO or START, with balanced clocks.

`test-i2c-stock-timing.py` independently executes the shipped AArch64 setup
fragment and timing helpers in Unicorn, stopping before address, DMA and START.
It checks the Image SHA256
`71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f`
and stock DT compatibility bytes. The fragment at file offsets
`0xa1d538..0xa1db78` calls `i2c_set_speed` at `0xa208b4` and
`mtk_i2c_calculate_speed` at `0xa20ab0`. Only `_mcount` and `clk_get_rate`
are stubbed. Clock fixtures are 26, 65, 104, 124.8 and 136.5 MHz; rates are
100/400 kHz and 1/1.7/3.4 MHz; dividers are 1/2/5/8, on banks 0 and `0x100`.
180 register sets match exactly and both implementations reject the remaining
20 combinations. The 64 MHz boundary separately verifies the intentional
alternative encoding and timeout initialization. CI runs this after stock
extraction. No kernel ELF recovery tool is needed to repeat the test.

These tests establish software behavior and agreement with stock instructions.
They do not establish bus waveforms, physical clocks, FIFO/DMA operation,
interrupt delivery or successful communication with an r1 peripheral.

## AP interrupt gate and transfer errors

In the stock version-2 transfer path, `mt_i2c_do_transfer()` writes
`I2C_MCU_INTR_EN = 1` to `V2_OFFSET_MCU_INTR = 0x40` before START. This is separate
from the event mask at `0x08`. The MT8183 fallback never programmed this AP
interrupt destination. Depending on inherited firmware state, enabling event
bits alone can leave a transfer without an interrupt delivered to Linux.

The new MT6765 match supplies this register and writes the gate before START.
The native path uses the hardware's single-transfer or combined WRRD mode;
other supported SoCs retain their existing restart behavior. All MT6765 DT nodes use the single
`mediatek,mt6765-i2c` compatible; MT8183 is no longer advertised as a fallback.
The MT6765 register table also records the stock global `MULTI_DMA` offset
`0xf8c`, which the channel setup passes to secure firmware.

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
read/write/combined DMA cases, every terminal fault, allocation/mapping failures
and gate ordering. MT8183 regression checks retain its two-stage repeated starts
and master-code restart handling. The DMA model deliberately keeps a failed transfer active
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

## AP channel implementation

The SoC DT supplies `mediatek,secure-id` for controllers 2, 3, 4 and 6.
The driver accepts that property only for MT6765 and those IDs, selects the
`0x100` AP bank, and keeps DMA at the first channel in the existing resource.
I2C0/1/5 keep bank-zero access and make no firmware call. The compiled DT
validator checks each ID against its physical controller address.

Probe and resume enable clocks, request `(secure_id, 0xf8c, 2)` through SiP,
then initialize the controller. Any nonzero firmware result fails setup and
balances the clocks; resume does not make the adapter available on failure.
The driver tracks clock preparation so a suspend following failed resume does
not unprepare the clocks twice. Earlier EL3 caller checks can still reject the
request on real firmware; the Linux error path is exercised with stubs.

Register accessors take the AP offset for transfers and IRQs. Shared reset,
initial timing and arbitration recovery explicitly use bank zero. Each channel
transfer programs its own timing, I/O mode and full control configuration,
resets its AP DMA engine when DMA is used, clears FIFO with `0x5`, and clears all known status
bits. Its event mask is `0x129`: completion, arbitration loss, timeout and bus
error. ACK/NACK stays latched until an enabled event; the IRQ handler masks the
channel before waking the waiter. Allocation failures leave interrupts masked.

Both MT6765 banks advertise single writes, single reads and same-address
combined write/read transactions, with nonzero lengths. Combined transactions
use the hardware WRRD mode; START is `1`. The generic driver's software-driven
multi-restart bits are not used on either bank. Arbitrary multi-message
repeated-start sequences are rejected by the adapter quirks until their hardware
semantics are established. The vendor explicitly documents STOPs between its
separate transfers; exposing those as an arbitrary repeated-start operation
would violate the Linux I2C contract. Existing MT8183 support is unchanged.

After a completed channel error without BUS_ERR, recovery clears the channel
FIFO and resets only AP DMA. It does not reset the shared controller and disrupt
another owner's transfer. Incomplete or bus-error transfers use shared recovery;
a hardware fault kicks arbitration in bank zero. A software timeout only kicks
when the saved channel START reports ownership (bit 1), following the vendor
path. DMA is reset before unmapping failed transfers, and failed reads do not
copy bounce buffers back to callers. This models the stock recovery sequence;
physical DMA quiescence and concurrent CCU behavior remain untested.

The transfer harness now also executes 36 AP-bank read/write/WRRD cases, the
adapter entry point, masked NACK followed by completion, allocation failures,
arbitration recovery, secure-ID parsing, firmware refusal, clock failure and
failed-resume recovery. It checks register addresses and untouched CCU bank
bytes under ASan/UBSan. Mutations redirecting AP writes to bank zero, ignoring
firmware errors, resetting shared state after a completed NACK, or omitting the
channel FIFO bit are rejected. These are software/MMIO-model tests, not board
acceptance results.

## Short transfers through FIFO

MT6765 now uses its eight-byte FIFO for short transfers on both banks. A WRRD
pair uses FIFO only if both lengths are at most eight; either longer message
selects DMA for the whole pair. This follows the stock driver and covers the
CST836's short register reads and the MT6370's small register accesses.

FIFO mode clears the controller's DMA, DMA-acknowledge and asynchronous-mode
bits. It uses byte accesses to the selected bank's data port and does not map,
start or release DMA buffers. Reads copy exactly the requested length only after
successful completion; failed reads leave the caller's buffer unchanged. Each
transfer refreshes control and length registers, including when changing between
FIFO and DMA. IRQs stay masked until buffer preparation is complete. Error
recovery retains the shared/controller arbitration rules described above.

`test-i2c-irq.py` exercises 108 FIFO cases under ASan/UBSan, with read/write
lengths 1/3/5/8, both banks, all terminal faults, buffer canaries and balanced
clocks. Additional cases cover FIFO-to-DMA-to-FIFO transitions and mixed 8/9-byte
WRRD pairs. It compiles the kernel's actual `i2c_check_for_quirks()` to check
accepted and rejected message sequences. Six mutations are rejected at runtime:
generic restart, retained DMA bits, a nine-byte FIFO limit, ignored auxiliary
length, copying failed reads and reading from the wrong bank.

`test-i2c-stock-fifo.py` independently executes the checksummed shipped kernel's
selected setup fragment, starting at file offset `0xa1d490`. It verifies the
length decision and stops long-message cases at `0xa1d4d4`, before DMA setup.
Short cases continue through the real timing helpers and FIFO setup to
`0xa1e938`, immediately after START. All 48 short cases match an exact MMIO-write
whitelist: control, timing, address, IRQs, lengths, byte FIFO writes, MCU gate and
`START=1`. Another 22 cases select DMA at the eight/nine-byte boundary. Only
`_mcount` and `clk_get_rate` are stubbed; context, clocks and MMIO are modeled.
The audit does not execute FIFO reception, DMA, IRQ delivery or a peripheral.
CI runs it after stock extraction. These checks establish software sequencing,
not a successful transfer on hardware.

## Remaining controller work

The timing path now has an independent stock-instruction comparison, but more
controller work remains:

* Confirm SCL frequency, setup/hold times and hardware timeout behavior with a
  logic analyzer, including parent-clock changes. Matching stock register
  programming does not replace electrical validation.
* Channel setup, firmware caller acceptance and transfer/recovery sequencing
  need tests on a device, including FIFO, DMA and CCU coexistence.
  Arbitrary repeated-start sequences are not implemented for either bank.
  The [touch audit](TOUCH.md) records the board evidence.
* Bus arbitration gates, reset behavior and 33-bit DMA need transfer tests.

The vendor source also contains a `DEBUGCTRL=0x28` write guarded by
`CONFIG_ARCH_MT6765`. Both the extracted stock configuration and the published
reference build select `CONFIG_MACH_MT6765` instead; `CONFIG_ARCH_MT6765` is not
defined. That inactive code is not evidence that the r1 needs this write, and
it has not been copied into mainline.

The r1 board enables I2C5 for the MT6370 power monitor at 100 kHz. It has no
vendor channel-offset requirement. I2C4 is also enabled for experimental touch probing; the other buses remain disabled.
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
