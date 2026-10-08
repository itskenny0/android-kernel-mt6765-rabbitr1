# LK and splash build

`build-lk.py` uses the pinned [mtklkzap](https://github.com/itskenny0/mtklkzap)
revision `0decfc224e7f47c98092e85d1174f23575cc4980` after stock firmware extraction.
It produces `dist/lk/lk.bin`, `logo.bin`, a splash preview, a build record and
checksums. Packaging consumes these files; CI builds and tests them before
creating the mtkclient ZIP. No device commands run during the build.

The input is **only** RabbitOS v0.8.293 LK, SHA256
`534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e`.
An unknown image is rejected before patching. The output keeps the original
864,000-byte length and MTK header. No new bootloader is compiled from source.

## Relock protection

The first patch uses mtklkzap's reviewed r1 profile to make `flashing lock`
return `FAILRelock blocked: restore complete stock firmware first`. It exits
before the confirmation screen, factory reset or any lock-state write. The
separate verifier checks the handler-only diff and actual Fastboot error path;
Unicorn tests execute the stock registration and the refusal instructions.
The build then applies the warning patches and verifies their diff against this
guarded intermediate image.

**Never relock until the complete stock firmware package has been restored,
including every LK slot and every other modified verified partition.** Patched
LK can fail signature verification and leave the system unable to boot. The
guard covers this LK's command handler; a different slot's loader or direct
`seccfg` writes through tools such as mtkclient can still relock the device.
The generated restore script replays saved backups; it does not establish that
the complete device firmware is stock or that relocking is safe.

See [mtklkzap's relock documentation](https://github.com/itskenny0/mtklkzap/blob/main/docs/RELOCK.md)
for the profile and test details. Unknown stock LK images are rejected; this
protection has not been hardware-tested.

## Warning patches

The upstream orange-state patch removes the warning text and its five-second
delay. Its dm-verity patch suppresses the corruption screen. Upstream's verifier
checks the exact 102 changed bytes and disassembles the affected control flow.
That check runs before the separate DT handoff patch below.

These patches suppress UI and delay behavior. They do not unlock the bootloader,
forge green state, or remove all signature enforcement. The flashing preparer
still requires an unlocked device and sets the AVB disable flags in its backed-up
vbmeta. Whether the preloader accepts this patched LK on the target device still
requires a hardware test. Retain a BROM recovery route and the original LK.

## Separate LK and Linux device trees

Stock LK applies the selected DTBO twice: once to its embedded `lk_main_dtb`,
then to the Linux DT from `boot.img`. The previous no-op DTBO broke the first
use by omitting the stock GPIO initialization and charger configuration.

The package now retains the complete stock DTBO. A ten-byte edit in the Linux
caller selects the already prepared mainline base instead of calling the shared
`dtb_overlay()` function. The early LK caller and shared overlay code are
unchanged. The Linux caller still checks the 512 KiB limit, copies the DT to its
existing destination and expands the FDT's totalsize for later runtime fixups.

Offsets below are **file offsets in the exact pinned LK**, not generic patch
locations or runtime addresses:

| Offset | Original | Replacement | Effect |
| --- | --- | --- | --- |
| `0x2138e` | `bl 0x211bc; mov r4,r0; cbnz r0,0x213ec` | `str.w r8,[sp,#8]; movs r4,#0; nop` | Use the prepared base DT and continue through the existing success path |
| `0x213da` | `ldr r0,[sp,#8]` | `movs r0,#0` | Do not free the borrowed DT pointer as an allocated overlay result |

At this call site `r8` contains the main DT, copied to an aligned allocation
when necessary. The caller already frees that allocation through `r5`; the
second edit prevents a double free. The overlay's temporary alignment copy is
still released through `r6`. The patch allocates no new memory and changes no
load addresses, stack layout or callee-saved registers.

`test-lk.py` executes the actual Thumb instructions of `prepare_kernel_dtb()`
(`0x212e8`) in Unicorn. It models external accessors, allocation, copying and
logging, and tests all 16 combinations of base/overlay alignment, allocation
failure, oversize rejection, pointer ownership, register preservation and output
bytes. The original caller is a control test that invokes the overlay function.
A stock DT merge also verifies the complete board data, including the 5,040-byte
GPIO initialization table. This is not a full LK or MT6765 hardware emulator.

This LK is paired with the mainline package: its Linux overlay bypass also
applies if given a stock boot image. **Restore stock LK together with stock boot,
DTBO and vbmeta when returning that slot to RabbitOS.** The generated restore
script includes all of them and the shared logo.

## Preserve Linux MMC pin states

The later Linux FDT path calls `update_mmc_status()` at raw offset `0x1c290`,
after the overlay/copy step. Despite its name, this function does not change
the MMC node's `status`. It finds the first `mediatek,mt6765-mmc` node, copies
its `pinctrl-names` into 14-byte stack slots, then unconditionally swaps slots
0/3, 1/4 and 2/5. When `readl(0x100056f0) & 0x6000` is zero, it writes the
swapped names back. The actual register value on the r1 has not been measured.

Our mainline node has one state, `default`. The unused slots contain prior
stack data, so LK can rename that state to an empty or stale string. Linux
then cannot select the intended default state by name. The vendor DT avoids
this path because its MMC compatible is `mediatek,msdc`; the historical UART
capture logs the failed lookup. Adding dummy states to mainline would merely
accommodate this vendor assumption.

The build replaces **only the call in the Linux FDT path**:

| Offset | Original | Replacement |
| --- | --- | --- |
| `0x1c290` | `ea f7 b4 fc` (`bl 0x6bfc`) | `00 20 00 bf` (`movs r0,#0; nop`) |

The result is unused by the following instructions. This preserves the
kernel-provided pin names, references and RAM/expdb storage gate. It changes
neither LK's own eMMC initialization nor the shared fixup routine. The patch
uses the same exact-stock checksum and before-byte checks as the overlay fix.
The LK build record marks `kernel_mmc_pinctrl_preserved`; packaging and flash
preparation require that mark and check the four instruction bytes, rejecting
older LK output even if a record incorrectly claims the fix.

`test-lk-mmc-fixup.py` executes the caller at `0x1c282..0x1c294`, the complete
stock fixup at `0x6bfc`, its string-swap helper and the shipped libfdt/libc
instructions. It also executes the weak-symbol availability check using the
actual GOT entry at `0xb7908`, confirming the stock call is reachable.
Only logging and the MMIO-register read are modeled. The 34
fixtures include both compiled boot profiles, four register patterns, zero
and named prior stack contents, the actual vendor FDT and a synthetic six-state
node. The original code reproduces the bad rename; the patched caller leaves
the expanded FDT byte-identical. An image with the old call restored fails
the preservation check. This is not a full LK boot or a physical eMMC test.

## Missing vendor SCP node

The Linux platform fixup at `0x4a40` calls `platform_fdt_scp()` at `0x14be8`.
That helper requires a node compatible with `mediatek,scp`, which is absent
from the mainline DT. A failed lookup returns 1; the wrapper stops before the
other fixups, and `boot_linux_fdt()` takes its fatal assertion at `0x1c25e`.
This happens whether LK's SCP-loaded flag is zero or one. The caller's linked
GOT entry at `0xb7554` confirms this is a reachable path.

The build replaces just the SCP subcall in the Linux platform wrapper:

| Offset | Original | Replacement |
| --- | --- | --- |
| `0x4a44` | `10 f0 d0 f8` (`bl 0x14be8`) | `00 20 00 bf` (`movs r0,#0; nop`) |

The wrapper continues to the SSPM, SPM, PLL and PMIC ADC fixups. Its SSPM
reservation status becomes `okay`; the missing vendor SPM/ADC nodes are already
handled without aborting. The remaining mainline hardware properties are
unchanged in both boot profiles. The SCP helper itself and LK's firmware
loading/private DT paths are unchanged. This patch neither starts nor stops
SCP and does not add a mainline remote-processor driver.

The LK build record marks `kernel_scp_fixup_bypassed`; packaging and flash
preparation check both the mark and the four instruction bytes. An older LK
cannot pass by claiming the feature in its metadata.

`test-lk-platform-fixup.py` executes the real Linux caller and weak-symbol check,
the wrapper and its callees with shipped libfdt/libc. The stock image reaches
the assertion; the patched image runs the remaining fixups. Both SCP-loaded
states and both mainline profiles are covered. A separate vendor-style SCP
node tests the unchanged helper's status updates. Restoring the old subcall
makes the mainline test fail.

The test also runs the later `fdt_memory_append()` (`0x32f4c`), mrdump setup
(`0x256b4`) and reservation-node writer (`0x30bb4`) with **controlled boot
arguments**. All five supplied reservations retain their 64-bit address/size
and mapping flags, including a synthetic range above 4 GiB. LK creates
`/debug-kinfo` before linking its reserved-memory phandle, so it does not need
a placeholder in the mainline tree. Existing hardware bindings remain intact.
Logging, verbosity and uncontended mutexes are modeled; the instructions that
traverse and change the FDT execute. These checks do not establish the actual
RAM map, firmware memory ownership, remote-processor state, or whole LK boot.

## Preserve the Linux console

The Linux caller at `0x1cb70` invokes `0x4a730`, which rewrites the first
`ttyS` suffix in the kernel command line using LK's logging UART. With the
preloader logging flag clear, it changes `console=ttyS0` to `console=ttyS1`.
With logging enabled, the result depends on the preloader's UART address.
That conflicts with the mainline package's explicit `ttyS0` console and
`serial0`/UART0 device-tree selection. Earlycon uses a separate MMIO address,
so early output alone would not establish that the normal console survives.

The build changes only that Linux call:

| Offset | Original | Replacement |
| --- | --- | --- |
| `0x1cb70` | `2d f0 de fd` (`bl 0x4a730`) | `00 20 00 bf` (`movs r0,#0; nop`) |

The shared UART helpers and LK's own console initialization are unchanged.
Build-record format 5 adds `kernel_console_preserved`; packaging and flash
preparation require both this flag and the exact instruction bytes.

`test-lk-linux-fdt.py` reproduces the stock rewrite and checks the patched
caller in 40 fixtures: two packaged profiles, both LK images, logging on/off,
and four UART addresses plus an unknown address. Restoring the old call fails
the command-line preservation assertion.

The same test runs the contiguous post-decompression path in `boot_linux_fdt`
from `0x1bd6c` to `0x1d4a4` for both packaged boot images. Shipped FDT and libc
instructions update `/memory@40000000/reg`, serialize boot metadata, append the
command line, check mblocks, write reservations and pack the final FDT. The
result must retain the chosen console, profile parameters, initramfs bounds,
hardware bindings, and all five controlled reservations, including one above
4 GiB. LK's `/memory` lookup accepts the unit-addressed node: the test requires
exactly one memory node and checks its rewritten address and size. This lets
the board follow the root-node schema without changing LK's memory helpers.
The stock DTBO selector also executes to initialize the overlay index.

Firmware state and the memory map are fixtures. Logging, mutexes, display
queries, a read-only lock-state accessor and elapsed time are modeled; SRAM
and security/SoC register reads use fixed inputs. Memory writes are restricted
to the FDT, stack and reviewed data fields. The test stops before charging,
secure-world and cache/MMU handoff. It does not validate physical RAM ownership,
firmware allocation/free paths, display DMA or a complete LK boot. Final FDTs
are saved as `out/lk-linux-fdt-{ram,expdb}.dtb` for inspection.

## Secure firmware handoff

`test-atf-handoff.py` checks the next interface using the stock `tee.img`
(140,944 bytes, SHA-256
`e6de1331346ea1df4a0b78106de5ec5886f6eda99270de5e23ac9e7ba7101b62`)
and both stock and patched LK. The exact digest enforced by the test is also
recorded in `out/atf-handoff-audit.json`.

LK's function at file offset `0x2d84` disables and drains GIC interrupts.
The separate secure post-init function is at `0x3760`. The tested callers use
these SiP interfaces:

| SiP call | LK arguments | Traced ATF behavior |
| --- | --- | --- |
| `0x8200010c` | Zero | Sets a software flag; the selected service performs no MMIO |
| `0x82000101` | Zero | Applies device-access permissions and related infrastructure settings |
| `0x82000115` | Kernel, FDT, zero, 64-bit flag | Prepares CPU power controls and a non-secure Linux entry context |

Eight LK fixtures cover the cached crypto-disable flag, success/failure returns,
and both images. The final jump's arguments must match both packaged boot
headers. Twelve ATF fixtures cover three initial MMIO patterns, EL2 present or
absent, and UART logging enabled or disabled. Shipped dispatch arms, permission
table loops, context construction, copying and zeroing execute. Repeated kernel
requests must preserve the first prepared context.

The resulting context contains the FDT in `x0`, zero in `x1` through `x3`, the
kernel entry address, masked interrupts, and EL1h or EL2h as selected by the
modeled CPU feature register. The kernel preparation writes CPU/cluster power
controls at SPM offsets `0x204` through `0x228` and `BYPASS_SPMC` at `0x2b4`.
It does not write `DIS_PWR_CON` at `0x30c` or directly stop the display engines.
These register names come from the official MT6765 `mtk_spm_reg.h`.

This is **not proof of a safe display handoff**. The AArch64
[boot protocol](https://www.kernel.org/doc/html/latest/arch/arm64/booting.html)
requires DMA-capable devices to be quiescent before entering Linux. GIC cleanup
and access-permission changes do not establish that condition. LK's normal
handoff has no observed direct call to `primary_display_suspend` (`0x107a4`);
its power-off path does. Display remains disabled pending teardown and panel
power work. No speculative shutdown call is added by this audit.

The ATF mapping resolves its linked GOT and relative addresses; it is a fixture,
not a live memory map. System-register reads, logging, MMIO and secure-context
restore are modeled. EL3 entry, exception return, physical cache coherency,
asynchronous firmware and DMA completion are outside the test. No host SMC
or hardware write is performed.

### Why the existing display suspend call is insufficient

`test-lk-display-stop.py` executes the stock module-table initializer, stop and
power-off dispatchers, and selected display callbacks. Eleven fixtures cover
both overlays in idle states 1/2 or permanently non-idle, mixed overlay states,
and an uninitialized display. Logging, delays, LCM suspend transport and MMIO
are modeled. Driver callbacks and their indirect dispatch execute unchanged.

The overlay power-off callback at `0xa648` calls reset at `0xa5e8`. Reset polls
`FLOW_CTRL_DBG` at register offset `0x240`. With both low bits held clear,
it waits 2,001 times for 10 microseconds, logs a timeout and returns zero.
The caller then requests clock gating at MMSYS `0x14000104` despite that timeout.
Both overlays reproduce this behavior. The top-level suspend function also
returns zero and clears its powered-state bookkeeping after either reset fails.

In the tested path after the modeled LCM suspend, RDMA's stop callback at
`0xb128` clears enable and interrupt registers without resetting or waiting for
completion. DSI's stop at `0xc500` changes the mode bits without polling BUSY.
The test holds DSI BUSY high throughout the path, but clock-gate requests and
PHY shutdown writes still occur. LCM transport may itself wait for commands;
that excluded stage is not evidence of completion after the later stop writes.

This reproduces the stock power-off behavior, which must not be treated as a
checked Linux DMA handoff. No call to that function has been added to the Linux
boot path. A replacement needs completion checks before clocks or memory
ownership change, with an error path that does not continue into Linux after
failed quiescence. Physical completion signals and the remaining panel supply
mapping still need a device test. The report is `out/lk-display-stop-audit.json`;
its successful execution means the limitation was reproduced, not resolved.

## LineageOS splash

The splash belongs to the shared `logo` partition, not inside LK. The build uses
mtklkzap's included LineageOS artwork and its 480x640 BGRA profile, with a centered
216-pixel-wide logo. The stock image has 60 slots; only duplicate splash slots
0 and 38 are replaced. The other 58 compressed images remain byte-identical,
and the stock MTK header is preserved apart from its payload length.

The pinned mtklogo v0.1.2 Linux x86-64 release performs the round trip. An
independent decoder checks exact BGRA pixels against `splash.png`. Hardware
panel orientation and rendering remain untested. Using this artwork does not
make the diagnostic Linux build a LineageOS ROM or an official LineageOS port.
