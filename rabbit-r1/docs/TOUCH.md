# Touch input

The CST836 driver is compiled and covered by host tests, but touch input is not
working hardware in this port yet. I2C4 remains disabled until its AP channel
routing is implemented and reviewed. No r1 or touch controller was accessed.

## Stock evidence

The official kernel at `8167c8c1087f057d2ef302fc93b47554291687ec` supplies the
reference implementation in
[CST8xx](https://github.com/rabbit-hmi-oss/android_kernel_rabbit_mt6765/tree/8167c8c1087f057d2ef302fc93b47554291687ec/drivers/input/touchscreen/CST8xx):

* `hynitron_core.h` selects CST836 at `0x15`, with CST328 at `0x1a` as a fallback.
  `hyn_ts_data_init()` switches only if reading two version bytes from `0xa6`
  fails. This does not establish which controller is fitted to every r1.
* `tpd_touchinfo()` and `hyn_read_touchdata()` read 15 bytes in five-byte chunks
  starting at registers 0, 5 and 10. `hyn_i2c_read()` uses separate master-send
  and master-receive calls, with a STOP between them.
* The report count is the low nibble of byte 2. Each six-byte contact starts at
  byte 3: event in X-high bits 7:6, ID in Y-high bits 7:4, and big-endian 12-bit
  X/Y. Events 0 and 2 mean active, 1 means released. There are at most two IDs.
* Reset is physically low for 20 ms, then high. The vendor firmware-info path
  waits 100 ms before the version read. The configured interrupt edge is falling.

The verified RabbitOS v0.8.293 merged DT and GPIO table supply:

| Property | Stock value |
| --- | --- |
| Bus | I2C4, controller `0x11011000`, DMA `0x11000400` |
| SCL / SDA | GPIO105 / GPIO106, function 1, open-drain |
| Bus frequency | 400 kHz; draft uses 100 kHz |
| AP register channel | `ch_offset_default = 0x100` |
| Touch address | `0x15`; the vendor node's `@1a` suffix is stale |
| IRQ / reset | GPIO0 / GPIO174 |
| Coordinates | 480 × 640, no inversion or axis exchange configured |

The CST816x driver in this fork handles one contact with a fixed 240-coordinate
range. The CST340 driver uses a different 16-bit register protocol and enters the
controller bootloader during probe. Neither can be made an r1 CST836 driver by
changing a compatible string. The new `hynitron-cst836.c` handles the vendor
CST836 protocol separately; it does not claim CST328 support.

## Implementation

`CONFIG_TOUCHSCREEN_HYNITRON_CST836` is built in. The board describes the primary
CST836 configuration beneath the disabled I2C4 bus. Reset is an active-low GPIO
descriptor; the driver asserts it during removal, probe failure and suspend.
Resume resets the controller and reads its firmware version before enabling the
IRQ. A version response is only a presence check, not a silicon-ID check.

Reports use Type B multitouch slots and generic touchscreen properties. The
driver validates the complete frame before reporting contacts: count, IDs,
duplicate IDs, reserved event codes, controller mode and active coordinates.
Absent or explicitly released contacts are released. Failed or short transfers
and malformed frames release existing contacts, avoiding a stuck press if the
lost packet was the release. This may interrupt a gesture on a transient error.
Pressure and gesture keys are not advertised without established semantics.

The driver keeps the stock transfer boundaries. It does not scan addresses,
enter programming mode, install firmware, or carry over the vendor debug and
auto-update interfaces. Touch supplies currently depend on firmware rail setup;
their regulator mapping and the fitted controller need board confirmation.
Wake gestures and hardware suspend/resume are unverified.

## Remaining bus work

The stock host driver initializes shared registers in channel 0, then accesses
transfer registers through the configured channel offset. I2C4 uses `0x100` for
AP and reserves `0x200` for CCU. Its DMA channel offset defaults to zero.
Changing only the DT register base would also redirect the shared initialization
and is not equivalent to the vendor behavior.

On resume, the vendor driver asks ATF to set shadow-register mode through
`MTK_SIP_I2C_CONTROL(id, 0xf8c, 2)`. Its probe path assumes the firmware has
already established that mode. The current mainline MT8183 fallback has no
instance channel offset; its `MULTI_DMA` debug register is at `0x8c`, not the
vendor global `0xf8c`. Routing, reset/arbitration, DMA ownership and firmware
state must be resolved before enabling this bus. See [I2C.md](I2C.md).

## Validation and acceptance

`scripts/test-cst836.py` compiles the production transport, decoder, reporting
and PM callbacks with I2C/input/GPIO stubs and AddressSanitizer/UBSan. Fixtures
cover two contacts in reversed ID order, disappearing contacts, explicit UP,
empty frames, all 12-bit coordinate values, invalid count/IDs/duplicates/mode,
reserved events, panel bounds, and every short or failing transfer. PM checks
exercise reset timing, release on suspend and failed resume without IRQ enable.
Another suspend/resume after a failed resume must recover without nesting IRQ
disables. Mutations removing count bounds, accepting short reads or breaking
this IRQ recovery are each rejected by the tests.
This tests callback logic, not Linux input-core behavior or physical I2C timing.

`scripts/test-r1-touch.py` compares the compiled DT against stock addresses,
resolution, GPIO muxes and polarity evidence, and checks that I2C4 is still
disabled. The driver is also compiled for AArch64. CI runs both tests.

The CST836 binding documentation, compiled example and r1 touch node pass
schema checks. Building the shared schema cache still reports the existing
`mediatek,software-role-switch` missing-type warning, and the optional yamllint
step is unavailable in this workspace. Full-board validation still has the
48 diagnostics recorded in [VALIDATION.md](VALIDATION.md).

Before touch is accepted: implement the host channel routing, identify the
controller on a device, verify supplies and reset/IRQ waveforms, capture raw
reports, then exercise all screen corners, two fingers, drag/release and bus
error recovery through evdev. Confirm suspend behavior before enabling it for
the system. CST328 hardware needs its own driver/DT selection.
