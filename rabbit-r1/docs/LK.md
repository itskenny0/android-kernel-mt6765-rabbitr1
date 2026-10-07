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
