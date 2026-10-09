# GitHub Actions

The [haretic workflow](../../.github/workflows/rabbit-r1.yml) runs on pushes to
`rabbit-r1/**` and `mt6765-devel`, on pull requests, and on manual dispatch.
It builds the exact event commit, including GitHub's test merge commit for a PR.
An active run finishes when the branch is updated. GitHub retains the newest
pending run for that branch, so frequent pushes do not prevent complete builds.

The Ubuntu 24.04 runner puts the checkout, downloads, caches and build outputs
under `/rabbitr1`. Build tools are installed on the disposable runner. No local
host packages or device partitions are changed by this workflow.

Pinned HTTP downloads allow three attempts, with one- and two-second delays
after transient transport failures. Every completed download must match its locked
size and SHA-256 before publication. Checksum mismatches, certificate errors
and permanent HTTP errors stop immediately; existing files are preserved.

Before compiling, CI prepares the separate patched mtkclient source from its
pinned archive, verifies every file, and tests the [bulk transport changes](MTKCLIENT.md).
The checks use actual extracted methods with scripted endpoints and PyUSB source
fixtures. They do not import USB or discover devices. Preparing that source does
not migrate an existing diagnostic archive or establish physical flash safety.
New packages bundle the pinned offline checker, transport manifest and patch.
Their generated flash/restore scripts verify those inputs and check the separate
patched source before every mtkclient process, including GPT and readback.

The workflow:

1. Installs the tracked workspace tools and fetches pinned build inputs.
2. Checks the patch series, MT6357 mappings, MT6370 current measurement and
   backlight register/brightness behavior, I2C timing, transfer/IRQ and channel
   setup/recovery, DSI transfer results, CST836 transport/input callbacks,
   bounded expdb mapper and log decoder.
3. Builds the AArch64 kernel, device tree, modules and static diagnostic initramfs.
   It also compiles the DSI host, RDMA, OVL, plane, CRTC, DDP dispatch and DRM driver objects, which are not enabled
   in the boot profile.
4. Inspects stock metadata, compares the r1 power-device wiring against the
   stock FDT, checks touch wiring and I2C4 firmware identity, emulates the
   stock ATF I2C service, compares timing registers with the shipped kernel,
   audits the shipped FIFO setup, eight/nine-byte DMA selection and stock
   backlight setup instructions, and compares the panel driver with shipped
   init/suspend instructions. Stock LK power callbacks are checked across
   register values and failed reads, and the shipped kernel's display-bias
   stubs are executed; neither establishes panel supply wiring.
   Native DSI timing, pixel formats and lane/clock
   settings are compared with selected stock setup instructions; invalid modes,
   early power failures and MT8183 behavior are checked with host stubs.
   DSI IRQ acknowledgements and the CPU RX/RACK sequence are compared with
   shipped instructions. Native command encoding, response parsing, signal and
   timeout recovery, probe ordering, power references and IRQ/clock ownership
   run through production callbacks with modeled hardware and scheduling.
   Native DSI sleep/wake/reset/mode writes are compared with complete shipped
   routines; stock power callbacks check reserved-bit preservation and clock
   calls. Native wake/shutdown failure cleanup and engine drain are exercised.
   Native DSI acquisition also runs through the production CRTC startup path:
   component/clock/PHY failures, reverse unwind, bridge gating, power-reference
   ownership, panel-unprepare access and successful retries are checked.
   CRTC events run through DRM commit ordering and vblank reference/completion
   helpers with CMDQ compiled both in and out. Failed startup, off-state guards,
   event ownership, explicit retries and shutdown are checked with models;
   physical synchronization remains unverified.
   GCE flush tests run the controller with the mailbox core and atomic polling
   helper. They cover callback-time buffer frees, failed suspend/reset, retry,
   runtime-PM balance and millisecond deadlines. A queue probe also checks that
   admission precedes controller acceptance and a failed flush can submit work.
   Direct acceptance, packet validation, IRQ retirement, stop retries, suspend
   refusal and managed clock cleanup are checked as well. The combined CRTC/GCE
   harness checks captured events, completion racing submission, rejected work,
   watchdog expiration, reuse, plane-disable fallback and cancellation before
   power-off/destruction. MMIO, DMA, PM, scheduling and unrelated services remain
   modeled; a permanent reset failure intentionally blocks unsafe teardown.
   A pthread harness races the production IRQ against forced system suspend at
   five points, including between channels. It executes the core force-PM
   helpers and checks the IRQ gate, drain, suspend failures and resume failures
   with PM enabled, plus IRQ access with PM disabled. Clocks, MMIO, PM bookkeeping
   and IRQ-core synchronization remain models.
   Native IOMMU fault tests separate L2 and main sources, snapshot both slaves,
   check posted-clear readback, and deliver faults arriving during callbacks on
   the next invocation. Inactive and non-translation records must not be read;
   MMIO, status latches, callbacks and TLB completion are modeled.
   The native MT6765 PHY is compared with stock setup/shutdown sequences,
   including calibration restoration, PLL divider boundaries and clock cleanup.
   MMSYS routing and mutex writes are compared with shipped instructions for
   the chosen DRM path; cleanup, legacy platforms and the compiled RDMA clock
   are checked as well.
   Native RDMA FIFO/QoS writes and reset transitions are compared with stock
   instructions. Clock/reset failures, IRQ-safe configuration, queued writes,
   input-mode changes and the native component/DT lookup are covered.
   Native RDMA/overlay teardown checks delayed reset completion, both RDMA
   timeout stages, recovery retries and persistent failures. Clock release must
   wait for reset success; IRQ draining and caller buffer ownership are modeled.
   The RDMA IRQ test compares event classification and selective acknowledgement
   with shipped instructions, then checks masked vblank, error diagnostics,
   clock-free probe, failure cleanup and synchronized IRQ shutdown with models.
   Both native overlay blocks are checked against stock FIFO/request, ROI and
   startup register values and stock IRQ classification. Mode bounds, inherited
   layer cleanup, reset/clock failures, queued writes, IRQ lifetime and the
   existing MT8192 behavior run under ASan/UBSan.
   RGB plane setup is compared with shipped instructions for all six physical
   layers, nine formats and three blend modes. A separate DRM address/opacity
   check covers all 16-bit alpha values, cropped reflections and padded rows.
   Packed-YUV edge clipping is compared with the shipped kernel through the
   production pending-address callback. Framebuffer offsets, native field and
   allocation bounds, and normal/async validation and visibility are checked.
   It then builds patched LK and the LineageOS splash with
   pinned mtklkzap, checks Fastboot relock refusal, and emulates the Linux DT handoff.
   The later MMC pin-name fixup runs with shipped libfdt/libc instructions on
   real mainline/vendor FDTs. Tests reproduce the original stale-stack rename
   and require the patched Linux caller to preserve both boot profiles.
   Platform fixup tests reproduce the missing-SCP boot assertion, check the
   corrected call path, and exercise later reservation writers with controlled
   boot arguments. Both packaging stages reject missing MMC/SCP/console fixes even if
   their metadata incorrectly claims the patch.
   Stock display teardown fixtures reproduce ignored reset timeouts and clock
   gating requests. The new handoff guard is assembled with ARM binutils and
   tested separately in 33 fixtures, including delayed completion and persistent
   failures; five altered instruction sequences must fail execution assertions.
   The final LK still refuses Fastboot relock. Both packaging stages reject an
   old hook, damaged guard payload or damaged relock prefix before flash files
   are produced. Physical DMA completion remains untested.
5. Builds both mtkclient boot profiles. Checks 40 console-rewrite fixtures and
   executes successive Linux FDT updates on the packaged images through final
   packing, with controlled firmware state. The resulting command line,
   initramfs bounds, hardware bindings and reservations must be preserved.
   It also traces LK's secure-call arguments and selected stock ATF dispatch
   arms in 8 LK and 12 ATF fixtures. Entry registers and repeat-call behavior
   are checked; EL3 entry/return and physical DMA handoff remain untested.
6. Checks boot headers, DT tables, module identity, target shell syntax, GPT and
   backup validation, AVB flags, checksums and repeat packaging.
   The 145 transfer/preflight/readback cases remain, with additional transport
   metadata, copy-binding and checker-failure tests. Recording checker/device
   processes verify check-before-command ordering and rejection; they do not
   import USB or substitute for the separate actual source-preparer tests.

Default package tests compare the images and modules with the current build
outputs. A historical host-tool refresh instead requires the explicit
`--reference-package-manifest` and `--reference-package-manifest-sha256` options.
That mode checks the independently pinned original manifest, kernel provenance
and all unchanged binary payloads before checking embedded kernel/module/ramdisk
bytes against the reference. It is not the default CI path and does not relabel
older payloads as a current kernel build. Existing archives require an explicit
refresh; installing the patched source alone leaves them unchanged.

`fetch-sources.py --profile ci` fetches mkbootimg, BusyBox, the stock firmware
archive, mtklkzap, mtkclient, the mtklogo binary and checksummed MT6357 register/ADC/gauge source
references. The ADC harness checks voltage units, channel requests, mux cleanup,
scaling, reset/probe errors and timeout locking; the driver is now linked into
the AArch64 kernel for battery measurements. The impedance harness checks MT6357 start/stop ordering,
data masking, bus failures and reset gating. The ADC harness also checks
binding-ID translation for all supported PMIC tables. The MT6370 limit harness
compiles the property setter and kernel range helpers to check rejected inputs,
rounding, register fields and bus-error propagation.
The current-transition harness checks the stock low-current workaround against
pinned Rabbit sources, both threshold directions, partial writes and cleanup
failures, concurrent setters, immediate callbacks during registration and failed
probe cleanup. It also checks charge inhibit/resume, request invalidation,
model-specific property exposure and stop-on-teardown, using the compiled stock
stop routine as a write/delay oracle. Early BC1.2 work is tested before and after
the power-supply handle is published. The registered shutdown callback, partial
IRQ setup, pending-work cleanup and property calls racing with shutdown are
also exercised. Regmap, time, IRQ synchronization, workqueue and device-resource
behavior are modeled; these checks do not validate physical charging.
Input initialization is checked across inherited register/selector values and
bus failures, including loop enable and settling before the pin limit is removed.
Zero-budget input isolation and explicit restoration run through property callbacks,
with failed writes, pending/running MIVR work, early publication, inherited state
and shutdown races. These checks do not establish USB suspend compliance.
The MUSB budget harness compiles the MediaTek power-supply callbacks and MUSB
dispatch, tests role changes and teardown, and interleaves delayed requests with
suspend and shutdown. Both build options and the legacy PHY path are covered.
It models PHY/MMIO/notifications and does not prove input-current enforcement.
The MT6357 current harness compares every raw value with the active stock gauge
at several calibrations. It runs the production read, latch cleanup, property
and probe code with the kernel's polling macros, a modeled clock/regmap and
pthread locks. Failed reads, partial writes, repeated recovery, timeout bounds,
concurrent callers and invalid probe settings are checked. The board wiring
test compares shunt/gain and thermistor calibration with stock DT units and
checks named ADC wiring, including the changed VBIF binding ID. The gauge harness
also compares temperature with compiled stock arithmetic, checks voltage units
and tests missing ADC inputs, positive IIO success returns, conversion errors,
malformed thermistor tables and registration-time callbacks.
It leaves the checked-out kernel
unchanged. The patch base is fetched separately for the patch consistency test.
The ordinary fetch command still provisions the complete development workspace.

Source archives are cached by the lockfile hash and verified on every run.
Build outputs are not cached. The cache and artifact actions are pinned to full
commit IDs. The workflow uses a read-only repository token and no stored secrets;
the public source checkout itself needs no credentials.

[mtklkzap's relock workflow](https://github.com/itskenny0/mtklkzap/actions/workflows/relock.yml)
also tests the pinned tool independently. It checks the official stock LK,
executes its Fastboot registration and refusal path, tests command-line write
protection, and confirms relock refusal survives the warning patches. These
checks run here as part of `test-lk.py` as well.

## Artifacts

Successful runs upload `haretic-r1-mainline-<commit>` with the kernel, DTB,
modules, configuration, symbols, build record, initramfs, patched LK, logo,
splash preview, mtkclient ZIP and
checksums. `haretic-r1-logs-<commit>` is uploaded even when a later step fails.
Both artifacts are retained for 14 days. Download them from the run's Actions
page and follow [FLASHING.md](FLASHING.md) before using a boot image.

The [first hosted run](https://github.com/haretic/android-kernel-mt6765-rabbitr1/actions/runs/37671533107)
passed in 17m55s at `98375f81dbc0b060aa04b8480868833b96779311`. Both uploaded
artifacts were downloaded and their file checksums verified. The record is in
[ci-first-run.json](research/ci-first-run.json). That historical artifact predates the [LK/DTBO fix](LK.md); use a run
that includes the LK build and emulation steps for the corrected package.

A green run means the build and listed offline checks passed. It does not
establish a working r1 boot, charging, eMMC persistence or panic recovery. The
known full DT-schema failures remain documented in [VALIDATION.md](VALIDATION.md);
CI currently gates the compiled DT's targeted consistency checks.

Manual dispatch appears in the GitHub UI once this workflow is on the default
branch. Pushes to `rabbit-r1/bringup` trigger it without changing the default
branch.
