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

Patch 0019 makes successful MediaTek DSI writes return the transmitted length.
Patch 0020 adds the r1 panel driver, binding, disabled graph and build setting.
The driver reproduces the stock sequence and GPIO45 reset, with transport error
handling. It relies on firmware-established supplies; analog PHY setup and
actual scanout remain unfinished. See `docs/DISPLAY.md`.

Patch 0021 documents the native MT6765 DSI compatible. Patch 0022 selects it,
adds stock-derived digital PHY and video blanking timings, corrects RGB666
selectors and sets the r1 non-continuous clock flag. It validates mode fields
and rate limits and stops host startup on PHY errors. The analog PHY/PLL,
remaining startup sequence and display routing still need work; the graph
remains disabled. See `docs/DISPLAY.md` for the selected-register comparison
and its limits.

Patch 0023 documents the native MT6765 PHY compatible and its firmware settings.
Patch 0024 adds its D-PHY backend, preserving LK calibration and reproducing the
stock PLL, bandgap and lane shutdown sequence. Shared hooks keep the reference
clock enabled through analog setup/teardown, with failure cleanup and the
existing backends' callback order retained. The r1 PHY stays disabled pending
the remaining display pipeline work and hardware validation; see `docs/DISPLAY.md`.

Patch 0025 corrects the native MMSYS route selectors, DSI mutex membership and
RDMA clock. Patches 0026/0027 add the native RDMA binding, FIFO/QoS setup,
reset sequence and input-mode configuration. Patch 0028 adds selective IRQ
acknowledgement and error diagnostics, avoids unpowered probe accesses and
synchronizes IRQ shutdown before clock disable. See `docs/DISPLAY.md` for the
stock-instruction comparisons and remaining display acceptance work.

Patch 0029 adds native bindings for the four-layer and two-layer MT6765 overlays.
Patch 0030 replaces their MT8192 fallbacks, adds stock-derived FIFO/request and
startup settings, clears inherited layers and background input, bounds reset
polling and mode dimensions, and handles error/vblank IRQs with clock ownership.
Plane formatting, firmware MMSYS teardown and physical display validation remain
unfinished; see `docs/DISPLAY.md`.

Patch 0031 fixes native MT6765 RGB plane opacity, constant-alpha format selection
and reflected crop addressing. It also clears stale RGB clipping/source-key state
and programs stock layer color and nonsecure input.

Patch 0032 includes framebuffer offsets in pending DMA addresses, validates
clipped geometry before component checks, and corrects async state/visibility
handling. Patch 0033 adds native packed-YUV pair expansion and clipping, with
field, row-pitch, allocation and 32-bit address bounds. Firmware handoff, IOMMU
and real pixel output still need work; see `docs/DISPLAY.md`.

Patch 0034 programs the display COLOR selector used by LK, alongside the
existing selector copied from Android. LK primary-init and scenario traces
now supplement the Android route comparison. Complete firmware handoff and
physical display validation remain open; see `docs/DISPLAY.md`.

Patch 0035 clears native overlay SBCH reuse controls after successful reset,
before IRQ enable. Mainline does not implement the layer-change tracking that
this optimization needs. The stock direct/queued cleanup paths and native
power-cycle ordering are checked; see `docs/DISPLAY.md`.

Patch 0036 sets the native DSI write-memory-continue command during lane
initialization. It follows the shipped RX/TX setup and removes another
dependency on inherited controller state. See `docs/DISPLAY.md` for the
dirty-register and lane-startup checks and their hardware limits.
