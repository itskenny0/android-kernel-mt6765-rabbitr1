# Mainline and LineageOS 24 porting status

Current charging integration: [CHARGING.md](CHARGING.md). The charger and board
policy are enabled experimentally and remain physically untested. The Android
charging APK, native services and combined SELinux policy passed their targeted
build and artifact checks. Boot/DTBO images, recovery policy and the real system
Binder library also built and passed offline checks. The complete SOC image
build passes eighteen offline artifact checks, and a separate raw super image
contains both slots. Android has not been flashed or booted; charging remains
physically untested. See [ANDROID.md](ANDROID.md) for the pinned inputs, build
evidence and remaining validation.

As of the inspected sources on 2026-10-07, the mainline fork reports Linux 7.1.0.
This is a development fork with downstream MT6765 changes, not an upstream Linux
release that already supports the rabbit r1. Diagnostic hardware testing began
on 2026-10-09: partition writes passed full readback, but neither boot attempt
established Linux execution. The host repeatedly reported USB descriptor and
address errors (`-71`), with no diagnostic ACM console. The USB clock-parent
update did not resolve that symptom. See the
[boot-attempt record](../tests/boot-attempt-20261009.json). This is not ready for
beta testing.

## Boot boundary

The supplied RabbitOS v0.8.293 boot image contains kernel 4.19.191, an Android v2
boot header, 2048-byte pages, a gzip kernel and a separate ramdisk. Addresses:

| Item | Stock value |
| --- | --- |
| Kernel load | `0x40080000` |
| Ramdisk load | `0x51b00000` |
| Tags / DTB load | `0x47880000` |
| Boot partition size in scatter | `0x02000000` (32 MiB) |
| DTBO partition size in scatter | `0x00800000` (8 MiB) |
| Header OS fields | Android 12; patch level 2023-07 |

These header fields are extracted facts, not a claim about the age of every
userspace component. Both the boot image's DTB section and `dtbo.img` use Android
DT tables (`0xd7b7ab1e`), each with one FDT entry. `inspect-stock.py` verifies
bounds, extracts the entries and applies the stock overlay to the **stock** base.
The mainline DTB is a raw FDT. It is not a drop-in replacement for the stock table.

LK looks up `/memory`; its shipped libfdt accepts the unit address in the r1
node's name, `memory@40000000`. Instruction tests run the Linux FDT updates
through final packing and verify that LK replaces the stock 512 MiB placeholder
with the supplied RAM layout without creating a duplicate memory node. The
placeholder does not describe the full RAM capacity. Dynamic stock
SSPM/SCP/connectivity reservations are retained, but runtime TEE/modem/loader
carve-outs cannot be recovered from a static image alone.

LK uses its separate embedded vendor DT for the early `/gpio@10005000` lookup
and charger initialization. The package preserves that tree. In the later Linux
DT, the vendor SCP fixup requires `mediatek,scp` and aborts when it is absent.
The patched LK skips that subcall while retaining the remaining platform fixups.
It also skips the vendor MMC fixup that corrupts the mainline pin-state names.
The Linux console remains the one selected by the boot image; LK's logging
settings no longer rewrite it to a different `ttyS` port.
Instruction tests reproduce these failures and verify the patches; see [LK.md](LK.md).
These fixes do not require vendor-compatible placeholder devices in the mainline
tree. The final FDT and UART log still need verification on hardware.

The existing vendor overlay targets vendor nodes and phandles. Applying it to a
mainline base may fail or misconfigure hardware. Packaging needs a deliberately
matched DT table/overlay strategy, verified addresses and a recovery route.
The experimental [mtkclient package](FLASHING.md) now wraps each mainline DTB
in the stock table format. It keeps the stock overlay for LK and includes a
patched LK that copies the mainline base at the Linux handoff without applying
that vendor overlay; see [LK.md](LK.md). It also provides backup-based preparation and restoration scripts.
The loader handoff remains untested. In particular,
`fastboot boot` support must not be assumed merely because a boot image exists.
Do not infer vendor_boot/init_boot support from template scatter entries: the
provided archive lacks those images and uses header v2.

The [boot-notes review](BOOT-NOTES.md) identified LK's separate embedded DT path.
The package preserves its stock GPIO and charger initialization. Emulator checks
exercise the patched Linux DT copy and cleanup; they cannot establish that all
later loader fixups or mainline hardware drivers work on an r1.

## Hardware work

The [community MT6765 matrix](https://wiki.nura.eco/wiki/MediaTek_Helio_P35_(MT6765))
is useful prioritization evidence. Its Y/P/N marks are not r1 acceptance results.

| Area | What this workspace establishes | Next acceptance evidence |
| --- | --- | --- |
| UART / PSCI | UART0 wiring, single-core diagnostic configuration | Full early log, correct RAM, then secondary CPU startup on r1 firmware |
| MT6357 | Driver corrections and mocked register tests; [details](POWER.md) | PMIC access/IRQ and measured rail behavior |
| eMMC | Corrected source-clock provider; disabled in RAM profile, enabled in expdb profile | Rails and tuning verified; repeated read-only I/O before relying on logging |
| USB | Peripheral DT, configfs functions, optional ACM initramfs and gadget power-budget source | Enumeration, console and VBUS/charger interaction |
| I2C | MT6765 interrupts, AP channels, native counters and hardware timeout; I2C4/5 enabled at 100 kHz; [details](I2C.md) | Firmware acceptance, clock/DMA/IRQ validation and repeated transfers on actual peripherals |
| Charging / battery | MT6370 MFD/ADC and hwmon enabled; MT6357 current/voltage/NTC measurements and MT6370 charge/input-path controls implemented; charger and board policy enabled experimentally, physically untested | Measurement and capacity validation, charge-policy/lease behavior, USB detection and sustained charging/disconnect tests |
| Display | r1 panel, selected native DSI timings and native PHY setup/shutdown match stock offline; backlight settings corrected; [details](DISPLAY.md) | Remaining host startup, clocks, routing and panel power, then measured PLL/link behavior, scanout and brightness |
| Touch | CST836 driver compiled and host-tested; stock wiring and I2C4 enabled for experimental probing; [details](TOUCH.md) | Host transfer validation, fitted-controller identification and evdev events on hardware |
| GPU | No working accelerated r1 stack established | Kernel/userspace compatibility and rendering tests; software rendering first |
| Wi-Fi / Bluetooth / GNSS / modem | No r1 mainline implementation established | Connectivity power/firmware transport and appropriate Linux subsystems |
| Audio / sensors / camera | Vendor sources are reference material | ASoC, IIO and V4L2/media integration, including the motor/hall hardware |
| Thermal / DVFS / suspend | Disabled or unavailable in initial build | Accurate thermal readings, rail transitions, idle and suspend/resume testing |

The active vendor panel source specifies 480x640, two DSI lanes and RGB888.
Its `PLL_CLOCK=130` is a vendor DSI setting, not automatically a DRM pixel clock.
The board-specific panel driver preserves the shipped sequence, and the native
host reproduces selected stock digital timing registers. A native PHY backend
preserves LK calibration and reproduces stock setup/shutdown in instruction tests.
Actual PLL/link behavior, controller identity and board supplies still need confirmation. The
display graph remains disabled; see [DISPLAY.md](DISPLAY.md). The fork's added Chipone touch code is
for another device and is not enabled as an r1 driver.

The MMC defect is independently identifiable: `CLK_TOP_MSDC50_0` and
`CLK_TOP_MSDC30_1` are topckgen IDs, while the original DTS paired them with
infracfg. Those numeric IDs address different clocks in infracfg. The patch
selects topckgen and the validator checks the actual DTB phandles and IDs. This
does not resolve every reported eMMC problem. The inferred VIO18 I/O supply is
marked in the DTS and still needs board verification.

Full DT schema validation is **not clean**. The refreshed report at
`logs/memory-node-full-schema.log` records 28 diagnostics for incomplete MT6765
bindings and inherited node/property mismatches (MMC, USB, display and other
blocks). The conservative MMC draft lacks the schema's second timing pin state.
These are recorded porting work, not silenced checks. The board-compatible
schema, root-node schema and binary DT consistency checks pass, but they do
not substitute for full schema conformance or a boot test.

## LineageOS 24.0

The pinned [official manifest](https://github.com/LineageOS/android/tree/lineage-24.0)
uses branch `lineage-24.0` and AOSP tag `android-17.0.0_r1`. The inspected
[mainline/common device tree](https://github.com/LineageOS/android_device_mainline_common/tree/lineage-24.0)
explicitly supports mainline-style kernels. Both are cloned and pinned in
`sources.lock.json`. The checkout and pinned mainline dependencies now support
the completed targeted and boot builds. As of 2026-10-09, system, system_ext,
product, vendor, super and vbmeta are building; their completion and image-size
checks remain outstanding.

The kernel enables Binder/binderfs, BPF/cgroups, SELinux, relevant filesystems,
encryption/verity and FunctionFS as groundwork. This is not a full Android 17
kernel configuration, VINTF, CTS or VTS compliance claim.

The intended integration sequence is:

1. Reach a stable RAM-only Linux shell and verify the loader memory map.
2. Establish storage, USB and at least framebuffer/DRM display access.
3. Validate the initial r1 Android device tree against `device/mainline/common`,
   the actual partition layout and the legacy boot packaging. Build userdebug with
   software rendering for the first UI. Keep HAL capabilities limited to what
   the kernel exposes.
4. Replace provisional health/power/input/audio choices with r1 implementations.
   Validate encryption, recovery, SELinux policy and hardware interfaces before
   enabling updates or treating the build as a daily system.

Specific integration traps already found in the pinned common tree:

* `MAINLINE_COMMON_KERNEL_PARAMS` selects `binder.impl=rust`; this kernel uses C
  Binder. Filter that parameter or deliberately build and validate Rust Binder.
* `TARGET_KERNEL_MIXED_MODE` defaults true for prebuilts; this build is not GKI
  mixed mode and must use false. Package the matching modules correctly.
* `TARGET_INITIAL_BRINGUP=true` selects SwiftShader, drmfb-composer, a provisional
  health HAL, no suspend, and development console/security settings. These are
  bring-up aids, not finished r1 hardware support.
* Mainline here has no `CONFIG_DM_USER`. Do not enable Android userspace virtual
  A/B snapshots before supplying an appropriate implementation or OTA design.
* Android 12 vendor GPU/camera/connectivity blobs and 4.19 `.ko` files cannot be
  assumed to operate with a 7.1 kernel and Android 17. Kernel UAPI and HAL
  dependencies must be replaced or explicitly bridged.

The common tree's `lineage.dependencies` lists drm_hwcomposer-upstream,
minigbm-upstream, linux-firmware-mainline, Mesa, hardware/mainline/common and
vendor/mainline. These dependencies are staged with the Android checkout;
[ANDROID.md](ANDROID.md) records their pins and intentional source changes.
Monitor workspace capacity during the full image build. No files or caches
should be moved outside `/rabbitr1` to work around space limits.

## Compressed memory

CRAM is not currently an r1 integration target. Gregory Price's
[LPC 2026 presentation](https://lpc.events/event/20/contributions/2424/)
describes compressed-memory hardware with directly mapped, byte-addressable
reads. This differs from a software compression algorithm or an ordinary
zRAM swap device. No compatible MT6765 hardware interface or implementation
has been identified in the checked sources or
[MediaTek's P35 specifications](https://www.mediatek.com/products/smartphones/mediatek-helio-p35).
That is a feasibility assessment, not a proof about undocumented silicon.
The presentation also says its benchmark tiers were DRAM-backed to isolate
fault overhead, and flags an unrelated reclaim stall in the zswap comparison;
those charts do not predict performance on the r1.

Evaluate [zRAM](https://docs.kernel.org/admin-guide/blockdev/zram.html) once
Android runs: compare no compressed swap with LZ4 and the default compressor,
then measure foreground latency, app reloads, memory pressure, actual RAM used
by compressed pages, CPU load and battery consumption. Choose size and reclaim
settings from those measurements. Mainline already enables MGLRU in this tree;
zRAM and zswap remain disabled in the diagnostic build. No CRAM patches or
speculative memory-performance claims are part of the current package.

## First device session

Obtain a full stock boot log, live DT, partition map, active slot/unlock state and
the exact firmware build. Establish recovery and the available temporary-boot
mechanism. Then test the RAM-only kernel/initramfs with storage still disabled,
capture `r1-report`, and compare its live DT/reservations and power state with
stock. USB ACM is optional (`r1.usb=1`); UART remains essential if USB cannot probe.
Bring up storage, power, display and remaining peripherals in that order with
logs for each change. Nothing in this workspace has been tested on an r1 yet.
