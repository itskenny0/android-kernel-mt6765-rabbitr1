# MT6357 retained RTC bytes

The MT6357 RTC provides a read-only, two-byte NVMEM view of the stock firmware's
retained gauge inputs. It does not validate those inputs or expose battery
CAPACITY. The gauge has no NVMEM consumer in this increment.

| Logical byte | r1 fixed cell | PMIC register | Field |
| --- | --- | --- | --- |
| 0 | `initialization@0` | `0x5a4` (`RTC_AL_HOU`) | bits 15:8 |
| 1 | `state-of-charge@1` | `0x5aa` (`RTC_AL_MTH`) | bits 15:8 |

The mapping and order come from the pinned stock `rtc-mt6397.c` field table and
reader. The stock MT6357 MFD uses RTC base `0x588`; the actual merged stock DTB
uses logical byte offsets 0 and 1. `tests/battery/mt6357-rtc-mapping.json` records
that provenance. Other compatible PMICs do not gain this provider.

The callback reads the requested registers under the existing RTC mutex shared
with alarm operations. It publishes the bytes only when all requested reads
succeed, preserving bus errors and leaving the output unchanged on failure.
It does not write a register, trigger WRTGR, change calibration, reset the gauge,
or alter alarm handling. Serializing Linux operations does not make the two
register reads atomic against PMIC or firmware changes.

NVMEM references can survive RTC driver unbind. The provider therefore stores
the NVMEM-pinned parent device rather than a pointer to the RTC allocation.
It takes `device_trylock()`, checks the exact bound driver, obtains its current
private state, and then takes the RTC mutex. A busy device returns `EAGAIN`,
which avoids deadlock while unbind removes sysfs. An unbound or different driver
returns `ENODEV` without touching old private data. A retained provider after
compatible rebind reads the current RTC instance; rebinding an unsupported PMIC
variant returns `ENODEV`. The RTC NVMEM helper assigns the same parent device
and RTC owner; NVMEM references retain the provider/parent and owner module.

`CONFIG_RTC_NVMEM` guards registration. The r1 configuration explicitly enables
RTC_CLASS, RTC_DRV_MT6397, RTC_NVMEM, NVMEM and NVMEM_SYSFS; artifact validation
requires them. With RTC_NVMEM disabled the RTC still registers, with no provider.
The fixed cells live beneath the r1 RTC's `nvmem-layout`; the existing inline
MT6357 RTC binding describes the logical layout.

Run the source suite with:

```sh
python3 scripts/test-mt6357-rtc-nvmem.py
```

After building, also check actual inputs:

```sh
python3 scripts/test-mt6357-rtc-nvmem.py \
  --config /rabbitr1/out/mainline/.config \
  --dtb /rabbitr1/out/mainline/arch/arm64/boot/dts/mediatek/mt6765-rabbit-r1.dtb
```

The suite compiles actual production callback, probe, alarm and trigger
functions with ASan/UBSan and pthreads. It checks all 16-bit register values
against the independent stock mapping, bounds, failures, publication rules,
configuration guards, concurrent alarms, teardown and rebind. Hardware,
registration and device-core lifetime rules are explicit mocks; kernel module
unload, actual retention and electrical operation still need runtime checks.
The default suite needs only hash-pinned stock C, not extracted firmware or
Unicorn. A separately compiled DTB verifies the cells without booting a device.

These raw bytes lack a verified age, same-battery provenance, paired counter
baseline and counter continuity. Stock validity/removal bits alone do not make
the retained percentage a usable estimator seed. No battery consumer, retained
seed assumption, RTC writes, CAPACITY, HEALTH or shutdown bypass is introduced.
