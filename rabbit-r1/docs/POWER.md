# MT6357 regulator investigation

The supplied MT6765 support table marks **MT6357 regulators partial** and
**MT6370 charging working**. These are different devices. The table describes
community testing on MT6765 devices; it is not proof that either works on an r1.

The fenolf-based fork has the same MT6357 regulator and PMIC-wrapper drivers as
the previously inspected evilMyQueen tree. Its additional work therefore does
not itself resolve the reported regulator issue.

## Corrections implemented

All register evidence below comes from Rabbit's published driver and register
definitions at `8167c8c1087f057d2ef302fc93b47554291687ec`:

* [Vendor regulator driver](../src/kernel/drivers/regulator/mt6357-regulator.c)
* [Vendor register definitions](../src/kernel/include/linux/mfd/mt6357/registers.h)
* [Modified mainline driver](../src/mainline/drivers/regulator/mt6357-regulator.c)

### SRAM selector write mask

The original mainline entries pass `0x7f00` as the voltage **write** mask for
`MT6357_LDO_VSRAM_CON0` and `MT6357_LDO_VSRAM_CON1`. The vendor defines
`RG_LDO_VSRAM_{PROC,OTHERS}_VOSEL_MASK = 0x7f`, shift **0**. Thus the write mask
is `0x007f`. The monitored voltage in `LDO_VSRAM_*_DBG0` uses shift **8** and
must retain `da_vsel_mask = 0x7f00`.

The patch separates these correctly. A requested selector such as `0x2a` must
update bits 6:0 in the write register; a readback of `0x2a00` decodes to `0x2a`.
This is a source-backed correction, not confirmation of the wiki issue's cause.

### MRV SRAM register mapping

Rabbit's driver tests `RG_TOP2_RSV0[15]`, whose address is `TOP2_ELR0` (`0x015a`).
When set, it exchanges the hardware register banks associated with VSRAM_PROC
and VSRAM_OTHERS. Mainline did not implement this.

The patch reads that bit once per probe and exchanges the enable, voltage-write,
voltage-readback and status register addresses. The masks are identical between
the two SRAM regulators; logical IDs, names and voltage ranges stay attached to
their consumers. Each PMIC gets a private copy of the descriptors, preventing a
probe/reprobe from changing a global table. Read errors propagate before any
regulators are registered. There are no direct PMIC writes in this helper.

Whether a particular r1 contains this revision has not been measured.

### Hardware status readback

`is_enabled` reads the software enable control. The new `get_status` reads the
DA enable indication used by Rabbit's driver:

| Type | Register family | Mask |
| --- | --- | --- |
| Bucks | `BUCK_*_DBG1` | bit 0 |
| SRAM LDOs | `LDO_VSRAM_*_DBG1` | bit 0 |
| Other LDOs | `LDO_*_CON1` | bit 15 |

VCN33 BT/Wi-Fi share one hardware status register. An ON indication is not a
voltmeter reading and cannot prove the expected voltage reaches the board.
This patch makes `/sys/class/regulator/regulator.*/status` useful alongside
`state`, `microvolts` and the regulator debug summary in `r1-report`.

## Checks and limits

`python3 scripts/test-mt6357.py` compiles the production tables and helpers in a
host harness with a read-only fake regmap. It independently derives expected
status registers/masks from the vendor header and checks all 32 entries, both
SRAM write/read masks, normal/MRV mappings, unchanged unrelated descriptors,
ON/OFF readback and bus-read failures. The test rejects a copy with the original
SRAM write masks restored. The AArch64 kernel build checks integration with the
real regulator/regmap interfaces. Neither check emulates the PMIC.

The bring-up configuration disables CPU frequency scaling, idle and suspend;
it does not exercise SRAM voltage transitions. `clk_ignore_unused` and
`regulator_ignore_unused` retain firmware state for initial observation and
must be removed after consumers and critical rails are verified.

The next power experiment needs a stock and mainline boot log, the live FDT,
regulator state/status/voltage summaries, the PMIC revision indication, and
measurements of the affected rail. First establish PMIC-wrapper transfers,
interrupts and consumer/supply bindings. Then test one rail at a time with its
known voltage constraints. No charging-current increases, guessed voltage
changes, or raw register write commands are included in this workspace.

MT6370 charger integration remains separate work: confirm the r1 I2C address,
GPIO interrupt routing, ADC and USB/charger detection, battery limits and thermal
policy. The stock config enables the MT6370 charger/backlight and MT6357 battery
drivers. Copying generic nodes from another MT6765 handset is insufficient.
The [I2C investigation](I2C.md) records the r1 wiring and fixes the inherited
clock-divider mismatch. Actual bus transfers and charging remain untested.

## Persistent logs

The stock log partition is `expdb` (20 MiB). The mainline expdb boot profile
uses pstore through a device-mapper view of its first 18 MiB. Rabbit's
`drivers/misc/mediatek/log_store/log_store.c` defines `EXPDB_LOG_SIZE` as 2 MiB
and writes at `file_size - EXPDB_LOG_SIZE`; its header offset is also bounded
within that tail. Our mapper excludes those last 2 MiB. This replaces old dump
contents in the first 18 MiB and uses the Linux pstore format, not AEE.

This adds diagnostics, not an eMMC or charging fix. The logger checks the
partition label and size, then loads matching `pstore_zone` and `pstore_blk`
modules after storage appears. The generic backend has no dedicated panic
writer. Early crashes and storage failures still require UART. See
[FLASHING.md](FLASHING.md) for setup, raw log extraction and restoration.
