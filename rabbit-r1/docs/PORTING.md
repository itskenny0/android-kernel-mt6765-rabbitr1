# Mainline and LineageOS 24 porting status

As of the inspected sources on 2026-10-07, the mainline fork reports Linux 7.1.0.
This is a development fork with downstream MT6765 changes, not an upstream Linux
release that already supports the rabbit r1. The board has no hardware test in
this workspace. A complete port cannot be established without a device.

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

The LK image contains an exact `/memory` lookup. The r1 DTS preserves this path
and the stock 512 MiB placeholder. That placeholder does not describe the full
RAM capacity. The loader must replace it with the actual layout. Dynamic stock
SSPM/SCP/connectivity reservations are retained, but runtime TEE/modem/loader
carve-outs cannot be recovered from a static image alone.

LK also refers to `/gpio@10005000`, `mediatek,scp`, and `scp_sramSize`, whereas
the mainline pin controller is beneath `/soc`. The Nokia example contains
compatibility placeholders; their exact effect on the r1 loader is unverified.
Capture the final FDT and a UART boot log before deciding on compatibility nodes
or a loader shim. The current r1 tree does not pretend these gaps are resolved.

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
| USB | Peripheral DT, configfs functions, optional ACM initramfs | Enumeration, console and VBUS/charger interaction |
| I2C | Explicit MT6765 interrupt handling and divider correction; I2C5 enabled at 100 kHz; [details](I2C.md) | Clock/DMA/IRQ validation, channel routing for I2C4 and repeated transfers on actual peripherals |
| Charging / battery | MT6370 MFD/ADC and hwmon bound; current-measurement bugs fixed; charger still disabled | Limits, battery gauge/temperature policy, charger detection and measured behavior |
| Display | Vendor active sequence uses ST7701-style commands despite its ili9883 filename | Panel identity, reset/power/backlight, DSI timings, then DRM scanout |
| Touch | CST836 driver compiled and host-tested; stock wiring described, I2C4 still disabled; [details](TOUCH.md) | AP channel routing, fitted-controller identification and evdev events on hardware |
| GPU | No working accelerated r1 stack established | Kernel/userspace compatibility and rendering tests; software rendering first |
| Wi-Fi / Bluetooth / GNSS / modem | No r1 mainline implementation established | Connectivity power/firmware transport and appropriate Linux subsystems |
| Audio / sensors / camera | Vendor sources are reference material | ASoC, IIO and V4L2/media integration, including the motor/hall hardware |
| Thermal / DVFS / suspend | Disabled or unavailable in initial build | Accurate thermal readings, rail transitions, idle and suspend/resume testing |

The active vendor panel source specifies 480x640, two DSI lanes and RGB888.
Its `PLL_CLOCK=130` is a vendor DSI setting, not automatically a DRM pixel clock.
The existing ST7701 driver is a possible starting point; controller identity and
board supplies still need confirmation. The fork's added Chipone touch code is
for another device and is not enabled as an r1 driver.

The MMC defect is independently identifiable: `CLK_TOP_MSDC50_0` and
`CLK_TOP_MSDC30_1` are topckgen IDs, while the original DTS paired them with
infracfg. Those numeric IDs address different clocks in infracfg. The patch
selects topckgen and the validator checks the actual DTB phandles and IDs. This
does not resolve every reported eMMC problem. The inferred VIO18 I/O supply is
marked in the DTS and still needs board verification.

Full DT schema validation is **not clean**. The log at
`logs/r1-dt-validate.log` identifies incomplete MT6765 bindings and inherited
node/property mismatches (I2C, MMC, USB, display and other blocks). The board's
intentional `/memory` loader-compatibility name also violates the current root
schema's address-suffixed memory convention; the conservative MMC draft lacks
the schema's second timing pin state. These are recorded porting work, not
silenced checks. The board-compatible schema and binary DT consistency checks
pass, but neither substitutes for full schema conformance or a boot test.

## LineageOS 24.0

The pinned [official manifest](https://github.com/LineageOS/android/tree/lineage-24.0)
uses branch `lineage-24.0` and AOSP tag `android-17.0.0_r1`. The inspected
[mainline/common device tree](https://github.com/LineageOS/android_device_mainline_common/tree/lineage-24.0)
explicitly supports mainline-style kernels. Both are cloned and pinned in
`sources.lock.json`; there is no full Android source sync or built system image.

The kernel enables Binder/binderfs, BPF/cgroups, SELinux, relevant filesystems,
encryption/verity and FunctionFS as groundwork. This is not a full Android 17
kernel configuration, VINTF, CTS or VTS compliance claim.

The intended integration sequence is:

1. Reach a stable RAM-only Linux shell and verify the loader memory map.
2. Establish storage, USB and at least framebuffer/DRM display access.
3. Create the r1 Android device tree using `device/mainline/common`, the actual
   partition layout and the proven legacy boot packaging. Build userdebug with
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
vendor/mainline. Fetch these alongside the Android tree at the integration stage.
At the initial kernel milestone about 226 GiB remained; a complete Android sync
and build require a new capacity check. No files or caches should be moved
outside `/rabbitr1` to work around space limits.

## First device session

Obtain a full stock boot log, live DT, partition map, active slot/unlock state and
the exact firmware build. Establish recovery and the available temporary-boot
mechanism. Then test the RAM-only kernel/initramfs with storage still disabled,
capture `r1-report`, and compare its live DT/reservations and power state with
stock. USB ACM is optional (`r1.usb=1`); UART remains essential if USB cannot probe.
Bring up storage, power, display and remaining peripherals in that order with
logs for each change. Nothing in this workspace has been tested on an r1 yet.
