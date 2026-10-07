# Patch series

Apply `mainline/*.patch` in filename order to the locked user-fork commit
`cceb223d0c2d7ce52fade7bb6b9ef679c560bea7`. `scripts/fetch-sources.py` does this
idempotently. Patches are also already present in `src/mainline`.

| Patch | Purpose |
| --- | --- |
| 0001 | Experimental rabbit r1 DTS, DTB target and board/vendor bindings |
| 0002 | Correct both MMC source-clock providers to topckgen |
| 0003 | Remove inherited mutex merge marker and correct the u8 table type |
| 0004 | MT6357 SRAM write mask, MRV mappings and hardware status reporting |
| 0005 | Standalone rabbit_r1_defconfig with block pstore modules |

0003 addresses the same two build defects already corrected in the old
evilMyQueen tree; it keeps the current fork's numeric component indices.
0004 follows Rabbit's vendor register definitions and revision workaround;
see `docs/POWER.md` for validation and limits.

`history/` preserves the earlier r1 work against the obsolete base for reference.
Do not apply that historical patch to the current series. Current changes are
committed on `rabbit-r1/bringup`; no change is represented as hardware-tested or
submitted upstream.

Patch 0017 adds native MT6765 FIFO transfers and uses the stock START sequence
on both banks. It keeps DMA for transfers longer than eight bytes and rejects
unsupported message sequences. See `docs/I2C.md` for stock-instruction evidence
and the remaining hardware checks.

Patch 0018 corrects MT6370 backlight field masks, mapping and shutdown polarity,
handles the vendor's early-revision restriction and preserves reserved brightness
bits. The r1 node remains disabled pending panel integration; see `docs/BACKLIGHT.md`.
