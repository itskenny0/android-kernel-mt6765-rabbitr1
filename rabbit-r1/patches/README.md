# Patch series

Apply `mainline/*.patch` in filename order to the locked user-fork commit
`cceb223d0c2d7ce52fade7bb6b9ef679c560bea7`. `scripts/fetch-sources.py` does this
idempotently. Patches are also already present in `src/mainline`.

Patch 0077 adds the default-off live battery-capacity experiment. It seeds the
stock-profile model before registration, checks cache freshness, and invalidates
continuity across observed power and lifetime events. Its error budgets are
uncalibrated. The shipping configuration remains unchanged; see
`docs/MT6357-SOC.md` and `tests/battery/soc-integration.json` for the tests and
remaining Android and hardware requirements.

Patch 0078 retains each charging-policy provider before looking up its power
supply. It rejects an unbinding provider and the MUSB child that reuses its
parent's OF node, and covers optional I2C Type-C providers. Charging limits and
policy are unchanged. `tests/battery/charging-supplier-lifetime.json` records
the source review, fault tests and ARM64 object check.

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

Patch 0037 uses write-zero-to-clear acknowledgement for native DSI status,
preserving interrupts that arrive after the handled snapshot. The stock
CPU/CMDQ acknowledgement paths and native late-event behavior are checked;
IRQ lifetime, the unbounded BUSY loop and complete panel reads still need
work. See `docs/DISPLAY.md`.

Patch 0038 selects per-port SMI translation control for MT6765 instead of the
MT8167 bitmap register. Patch 0039 removes the MT8192 LARB fallbacks and the
unmatched LARB2 clock name. Shipped port operations, native callback/clock
behavior and compiled DT wiring are checked. Actual IOMMU translation and
firmware DMA handoff remain unverified; see `docs/DISPLAY.md`.

Patch 0040 corrects native IOMMU throttling and controller setup, installs
the root before enabling table walks, flushes on first attachment and
restores zero-valued snapshots. Stock instructions and production software
lifecycle callbacks are checked; hardware translation, IRQ lifetime and
firmware handoff remain unverified. See `docs/DISPLAY.md`.

Patch 0041 selects the shipped MT6765 address format: 32-bit IOVAs, 34-bit
mapped physical addresses and tables in DMA32 memory. It validates complete
mapping batches against the physical limit and domain aperture. It also decodes
native fault addresses without treating status/reserved bits as address extensions.
The stock mapping/IRQ instructions and actual ARM v7s software page-table
operations are checked; physical DMA and IRQ ownership remain unverified.
See `docs/DISPLAY.md`.

Patch 0046 fixes GCE controller flush power references, cancellation ordering,
failed-stop ownership and timeout units. The CRTC and mailbox queue still need
a complete ownership and shutdown contract; see `docs/DISPLAY.md`.

Patch 0047 adds confirmed GCE submission and synchronous cancellation, assigns
page-flip events to their packets, separates watchdog expiry from ownership,
and drains before reuse, power-off or destruction. IRQ retirement, submission
errors, suspend and managed clock cleanup follow the same lifetime rules.
Permanent reset failure blocks teardown; hardware validation remains outstanding.
See `docs/DISPLAY.md` for the integrated tests and their limits.

Patch 0048 closes the GCE IRQ access gate and drains in-flight handlers before
forced system suspend removes clocks. Failed suspend restores the gate; failed
resume keeps it closed. Pthread tests execute the real controller callbacks and
core force-PM helpers with modeled hardware and IRQ-core synchronization.

Patch 0049 separates native IOMMU L2 and main fault sources, snapshots both
MMU slaves before clearing, and acknowledges before callbacks can trigger new
faults. A readback completes the posted clear. Empty and non-translation status
no longer produces a stale translation report. See `docs/DISPLAY.md` for coverage
and the remaining power, firmware-handoff and hardware requirements.
