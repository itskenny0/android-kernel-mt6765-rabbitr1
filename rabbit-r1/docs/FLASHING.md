# haretic for rabbit r1 with mtkclient

This package contains experimental Linux boot images, a matched DTBO image and
an offline flashing preparer. It is not an Android or LineageOS image. No r1 has
booted it yet. Header checks establish the image format, not whether LK can hand
off to this kernel. RAM fixups, firmware carve-outs, USB and eMMC still need a
device test. The initramfs exposes an unauthenticated development shell.

**Do not relock the bootloader before restoring the complete stock firmware
package, including every LK slot and all other modified verified partitions.**
Patched LK can fail signature verification and leave the device unbootable.
This package blocks the patched LK's Fastboot lock command. A different LK slot
or a direct `seccfg` write can still relock it. The backup restore script below
is not proof that the whole device is stock or safe to relock.

The package includes a patched LK and retains the stock DTBO for LK's own board
setup. The Linux handoff bypasses the vendor overlay and preserves mainline's
MMC pin states instead of applying LK's vendor-specific name swap. It also
skips the missing vendor SCP node requirement that otherwise aborts boot, and
preserves the Linux console instead of replacing it with LK's logging port. The warning patches and
LineageOS splash are built with pinned mtklkzap tools; see
[LK.md](https://github.com/haretic/android-kernel-mt6765-rabbitr1/blob/rabbit-r1/bringup/rabbit-r1/docs/LK.md).
The handoff is checked by emulation, but patched LK acceptance and booting still
need a device test. Restore LK and boot together when returning to RabbitOS.

Before entering Linux, patched LK stops the supported primary display path
and checks overlay/DSI reset completion. Failed completion or an unsupported
display state halts boot with `R1: DMA handoff failed` in LK's logger; a watchdog
may reset the device. Kernel `expdb` logging has not started at that point.
This guard has passed emulation tests, but has not run on an r1. Native Linux
display support remains disabled.

| Image | Use |
| --- | --- |
| `boot-expdb.img` | eMMC enabled; kernel console logs written to `expdb` after storage starts |
| `boot-ram.img` | eMMC disabled; RAM shell and UART/USB diagnostics without persistent logs |
| `dtbo.img` | Stock overlay retained for LK board initialization |
| `lk.bin` | Patched stock LK: relock guard, mainline DT handoff, orange-state and dm-verity warning removal |
| `logo.bin` | LineageOS splash in the shared logo partition |

Both boot images use the RabbitOS v0.8.293 v2 header, load addresses, 2048-byte
pages and a one-entry Android DT table. Boot images are padded to 32 MiB and DTBO
to 8 MiB. The old OS-version fields are retained for loader compatibility; they
do not describe a new Android build. The mainline boot image and stock DTBO must
be used with the packaged patched LK.

## Prepare the host and device backups

Keep the workspace, extracted package and backups under `/rabbitr1`. The pinned
[mtkclient v2.1.4.1 archive](https://github.com/bkerler/mtkclient/releases/tag/v2.1.4.1)
must be retained at `/rabbitr1/downloads/mtkclient-v2.1.4.1.tar.gz`. Prepare the
separate patched source using the checker, manifest and patch bundled with this
package. The original `/rabbitr1/src/mtkclient` stays unchanged.

The patched source is published in [haretic/mtkclient](https://github.com/haretic/mtkclient)
on `haretic/rabbit-r1`. Commit
[`46d2976`](https://github.com/haretic/mtkclient/commit/46d2976cd901e3a77478165de5d41afeed27e301)
contains the same six source changes pinned by the current transport manifest,
including the complete GPT export. The package preparer continues to reproduce
those changes from the pinned upstream archive and bundled patch.

```sh
cd /rabbitr1
source scripts/env.sh
(cd dist/mtkclient && sha256sum -c SHA256SUMS)
python3 dist/mtkclient/prepare-mtkclient.py \
  --manifest /rabbitr1/dist/mtkclient/mtkclient-transport.json \
  --patch /rabbitr1/dist/mtkclient/mtkclient-transport.patch
python3 -m venv toolchains/mtkclient
toolchains/mtkclient/bin/pip install -r src/mtkclient-haretic/requirements.txt
```

If `/rabbitr1/src/mtkclient-haretic` already exists, add `--check` to the preparer
command to verify it. The tool never overwrites or repairs an existing tree.
Keep `PYTHONDONTWRITEBYTECODE=1`, as set by `scripts/env.sh`; additional files,
including bytecode caches, fail the source check. These commands install host
dependencies but do not access a device.

The [transport patch](https://github.com/haretic/android-kernel-mt6765-rabbitr1/blob/rabbit-r1/bringup/rabbit-r1/docs/MTKCLIENT.md)
stops guarded bulk operations after uncertain transfers and propagates those
failures through XFlash reads and writes. It does not validate every DA status
or establish physical storage safety. Old packages and prepared scripts do not
gain these changes automatically; prepare new scripts from this package.

The guarded XFlash reader publishes a dump only after the complete selected
length, terminal status and local file writes succeed. It preserves an existing
dump on failure and creates successful dumps with mode `0600`. Use ordinary
directories and regular, single-link files; symlink paths and paths containing
`..` are rejected. Packet-size queries must succeed; the old fallback is removed.

An optional runtime file separates local preparation from execution on another
host. Keep the package, backups and `--out` under `/rabbitr1`; pass a local JSON
file with `--runtime` when preparing scripts for a host such as `/opt/rabbit`:

```json
{
  "schema": 1,
  "workspace": "/opt/rabbit",
  "directory": "/opt/rabbit/prepared/diagnostic-a",
  "archive": "/opt/rabbit/downloads/mtkclient-v2.1.4.1.tar.gz",
  "destination": "/opt/rabbit/mtkclient-haretic-cid",
  "python": "/opt/rabbit/venv-mtkclient/bin/python",
  "tmpdir": "/opt/rabbit/.tmp",
  "cache": "/opt/rabbit/.cache",
  "config": "/opt/rabbit/.cache/config",
  "data": "/opt/rabbit/.cache/data"
}
```

These are execution paths, not local filesystem lookups. They must be canonical
absolute paths within the chosen workspace. `/`, traversal and control
characters are rejected. Existing source trees next to the selected destination
are permitted; the workspace's original `src/mtkclient` remains protected.

Preparation records the runtime in `plan.json` and `runtime.json`. Both scripts
bind the latter's exact hash. A local preview prints the commands without
entering or inspecting the remote directory. When explicitly run with `--write`
on the execution host, each script enters that directory, checks its files and
runtime paths, and checks the pinned source before every mtkclient invocation.
Copy the complete prepared directory there and supply the exact pinned archive,
source tree, interpreter and existing temporary/cache directories first.
Directory and archive symlinks are rejected; a venv interpreter may link to its
base Python. The metadata selects that interpreter path, not its executable bytes
or installed dependencies.

The bundled source checker accepts `--workspace /opt/rabbit` together with
explicit `--manifest`, `--patch`, `--archive` and `--destination` paths. Its source
hash/mode checks and refusal to overwrite an existing tree are unchanged.
Runtime preparation requires the matching workspace-aware checker in the
package; refresh only the host tooling in a separately verified package before
using this option. Omitting `--runtime` keeps the existing `/rabbitr1` behavior.
This option grants no device action or bootloader-state assertion.

The runtime may also include `expected_emmc_cid`, set to an independently
observed 32-digit hexadecimal eMMC CID. Preparation normalizes it to lowercase
and appends `--expected-emmc-cid` after every generated `gpt`, `r` and `w` command.
The [CID guard](MTKCLIENT-CID.md) checks a fresh report inside that same connected
DA process before dispatch. GPT matching verifies only the header and partition
layout. The expected CID is device-specific metadata for the private prepared
directory; do not use the synthetic values from tests or commit a device's CID.

Host USB access and the connection sequence must work before attempting a write.
Follow the [mtkclient usage guide](https://github.com/bkerler/mtkclient/blob/v2.1.4.1/README-USAGE.md)
and Rabbit's [flashing documentation](https://github.com/rabbit-hmi-oss/community-wiki/blob/main/docs/flashing.md).
No host driver or udev installation is performed by these scripts.

Use an **already unlocked** r1. The package does not unlock it, change `seccfg`,
wipe user data or bypass an account lock. Setting vbmeta flags invalidates its
old signature and requires the unlocked bootloader to permit verification being
disabled. It does not unlock the bootloader itself.

Record the current slot before entering the MediaTek connection mode. If
fastboot is available, `fastboot getvar current-slot` is a read-only way to check.
Choose `a` or `b` explicitly; the preparer does not guess or activate a slot.
The example below uses `a`; replace it with the slot you intend to test.

When the device is connected in a mode accepted by mtkclient, these commands
read the GPT and full partitions. Use a new backup directory for each device:

```sh
cd /rabbitr1
source scripts/env.sh
mkdir -p backups
mkdir backups/r1-001
cd backups/r1-001
mtk() {
    python3 /rabbitr1/dist/mtkclient/prepare-mtkclient.py --check \
      --manifest /rabbitr1/dist/mtkclient/mtkclient-transport.json \
      --patch /rabbitr1/dist/mtkclient/mtkclient-transport.patch || return
    /rabbitr1/toolchains/mtkclient/bin/python /rabbitr1/src/mtkclient-haretic/mtk.py "$@"
}
mtk printgpt
mtk gpt .
while read -r part length; do
    mtk r "$part" "$part.img" --parttype user --offset 0x0 --length "$length"
    test "$(stat -c %s -- "$part.img")" -eq "$((length))"
done <<'PARTITIONS'
boot_a 0x2000000
dtbo_a 0x800000
vbmeta_a 0x800000
lk_a 0x100000
logo 0xb00000
expdb 0x1400000
para 0x80000
seccfg 0x800000
PARTITIONS
sha256sum *.img gpt.bin > BACKUP-SHA256SUMS
```

These lengths come from the stock v0.8.293 layout. Use a fresh directory and
read each partition separately: mtkclient v2.1.4.1 reuses the first partition's
length in a grouped read. Its return code alone does not establish a successful
read or write. The preparer checks the saved GPT and required backup sizes.

Retain these backups. Both `logo` and `expdb` are shared by both slots. The logging build replaces
old AEE dump contents in its first 18 MiB. The last 2 MiB are excluded from our
mapping because the stock `log_store` uses that tail. This is not a claim that
the stock bootloader or AEE will preserve our pstore data during a later crash.
The release's file named `gpt_backup.bin` is not relied on as a separately read
secondary GPT; the preparer validates `gpt.bin`'s primary header and entry CRCs.

## Prepare and review a write

From the generated package directory:

```sh
cd /rabbitr1/dist/mtkclient
python3 prepare-flash.py prepare \
  --backup /rabbitr1/backups/r1-001 \
  --out /rabbitr1/prepared/r1-001-a \
  --slot a --profile expdb --bootloader-unlocked
bash /rabbitr1/prepared/r1-001-a/flash.sh
```

The default preview does not access USB. `--bootloader-unlocked` records your
verification of the device state; it is not an unlock command. The preparer
checks the live GPT dump, full backup sizes, package hashes and the selected
slot's LK, DTBO and shared logo payloads against v0.8.293. Different firmware
stops preparation for inspection. It patches only AVB flags in your backed-up
vbmeta and preserves the trailing bytes of your LK partition. `plan.json` records the slot, layout and input paths.

Use `--profile ram` and a different output directory for the storage-disabled
image. That profile is useful for the first UART session. The expdb profile is
the one that enables persistent kernel logging.

When ready to test the chosen slot on the connected device:

```sh
bash /rabbitr1/prepared/r1-001-a/flash.sh --write
```

Before each mtkclient process, the script checks its bundled transport inputs
against the admitted package hashes and verifies the entire patched source
tree. A failed check stops before that process starts; it never falls back to
the original source tree.

The script first checks the live GPT, LK and current contents against the
backups. It writes `boot_a`, `dtbo_a`, `vbmeta_a`, the shared `logo`, then `lk_a`
(or the selected `b` equivalents). LK is written last. After each write it reads
that complete partition into a fresh file and compares every byte before
proceeding to the next partition. It does not write preloader, GPT, super, userdata or
calibration partitions. It does not reboot
or change the active slot. Confirm that the intended slot will be booted before
leaving the connection mode. A failed boot may cause the stock A/B bootloader
to fall back to another slot; record that behavior rather than reflashing both.

## Read logs and restore

Once Linux reaches the initramfs, `r1-report` shows logger setup status.
`/sys/fs/pstore/console-pstore_blk-0` contains the previous boot's recovered
console when available. The current kernel console is written continuously
through pstore; it is not a userspace copy of `dmesg`. Setup checks the GPT label,
device number and exact 20 MiB size, and maps only the first 18 MiB.

For offline recovery, reconnect with mtkclient and dump the full partition to a
new file before booting stock firmware or another mainline attempt:

```sh
cd /rabbitr1
source scripts/env.sh
test ! -e backups/r1-001/expdb-after.img
python3 dist/mtkclient/prepare-mtkclient.py --check \
  --manifest /rabbitr1/dist/mtkclient/mtkclient-transport.json \
  --patch /rabbitr1/dist/mtkclient/mtkclient-transport.patch
toolchains/mtkclient/bin/python src/mtkclient-haretic/mtk.py r expdb backups/r1-001/expdb-after.img --parttype user --offset 0x0 --length 0x1400000
python3 dist/mtkclient/decode-expdb.py backups/r1-001/expdb-after.img \
  --out /rabbitr1/logs/expdb-attempt-001
```

The decoder checks bounds and reconstructs partial or wrapped console/pmsg
rings. It decodes this package's layout, not MediaTek AEE or the separate panic
record slots. Keep the raw dump for further analysis.

Persistence starts only after eMMC, device mapper and the pstore modules work.
The console ring is 1 MiB, with 64 KiB for pmsg and 64 KiB slots for kernel dumps
in the remaining mapped space. Earlier messages still in the printk ring can
be replayed when pstore registers. There is no dedicated panic-write callback
in this storage path: fatal early crashes, a dead eMMC controller, sudden power
loss or interrupt-context messages not yet flushed can be missing. UART is
still needed for early bring-up. Do not use this logging image as a daily system;
continuous flash logging also creates write wear.

Restore all five written partitions from the saved device backups:

```sh
bash /rabbitr1/prepared/r1-001-a/restore.sh
bash /rabbitr1/prepared/r1-001-a/restore.sh --write
```

Restore verifies the device GPT identity, then writes and reads back the original
boot, DTBO, vbmeta, shared logo and LK. It accepts a partially completed earlier
write and does not require LK to still equal its stock backup. It leaves `expdb` intact for diagnosis. After copying out logs,
restoring the original shared log partition is a separate optional operation:

```sh
cd /rabbitr1
source scripts/env.sh
test ! -e backups/r1-001/expdb-restored.img
python3 dist/mtkclient/prepare-mtkclient.py --check \
  --manifest /rabbitr1/dist/mtkclient/mtkclient-transport.json \
  --patch /rabbitr1/dist/mtkclient/mtkclient-transport.patch
toolchains/mtkclient/bin/python src/mtkclient-haretic/mtk.py w expdb backups/r1-001/expdb.img
python3 dist/mtkclient/prepare-mtkclient.py --check \
  --manifest /rabbitr1/dist/mtkclient/mtkclient-transport.json \
  --patch /rabbitr1/dist/mtkclient/mtkclient-transport.patch
toolchains/mtkclient/bin/python src/mtkclient-haretic/mtk.py r expdb backups/r1-001/expdb-restored.img --parttype user --offset 0x0 --length 0x1400000
cmp backups/r1-001/expdb.img backups/r1-001/expdb-restored.img
```

None of the device commands above have been run in this workspace; no test
device is available. Keep the original firmware archive as a separate recovery
reference. A byte-for-byte write verification is not a successful boot test.
