# rabbit r1 mainline with mtkclient

This package contains experimental Linux boot images, a matched DTBO image and
an offline flashing preparer. It is not an Android or LineageOS image. No r1 has
booted it yet. Header checks establish the image format, not whether LK can hand
off to this kernel. RAM fixups, firmware carve-outs, USB and eMMC still need a
device test. The initramfs exposes an unauthenticated development shell.

**Known loader compatibility issue:** the current no-op DTBO omits the GPIO
initialization table that the stock overlay supplies to LK's own device tree.
The DTBO strategy needs revision before a hardware flashing trial. The steps
below document the experimental tooling; a passing package check does not
clear this issue. See the
[boot-notes review](https://github.com/itskenny0/android-kernel-mt6765-rabbitr1/blob/rabbit-r1/bringup/rabbit-r1/docs/BOOT-NOTES.md).

| Image | Use |
| --- | --- |
| `boot-expdb.img` | eMMC enabled; kernel console logs written to `expdb` after storage starts |
| `boot-ram.img` | eMMC disabled; RAM shell and UART/USB diagnostics without persistent logs |
| `dtbo.img` | Matching no-op overlay; required with either mainline boot image |

Both boot images use the RabbitOS v0.8.293 v2 header, load addresses, 2048-byte
pages and a one-entry Android DT table. Boot images are padded to 32 MiB and DTBO
to 8 MiB. The old OS-version fields are retained for loader compatibility; they
do not describe a new Android build. Never mix the stock vendor DTBO with this
mainline kernel.

## Prepare the host and device backups

Keep the workspace, extracted package and backups under `/rabbitr1`. The source
release of [mtkclient v2.1.4.1](https://github.com/bkerler/mtkclient/releases/tag/v2.1.4.1)
is already downloaded and extracted at `/rabbitr1/src/mtkclient`. The release
has no attached binary installer. Dependencies can be installed into a local
virtual environment:

```sh
cd /rabbitr1
source scripts/env.sh
python3 -m venv toolchains/mtkclient
toolchains/mtkclient/bin/pip install -r src/mtkclient/requirements.txt
```

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
mkdir -p backups/r1-001
cd backups/r1-001
mtk=(/rabbitr1/toolchains/mtkclient/bin/python /rabbitr1/src/mtkclient/mtk.py)
"${mtk[@]}" printgpt
"${mtk[@]}" gpt .
"${mtk[@]}" r boot_a,dtbo_a,vbmeta_a,lk_a,expdb boot_a.img,dtbo_a.img,vbmeta_a.img,lk_a.img,expdb.img
"${mtk[@]}" r para,seccfg para.img,seccfg.img
sha256sum *.img gpt.bin > BACKUP-SHA256SUMS
```

Retain these backups. `expdb` is shared by both slots. The logging build replaces
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
slot's LK payload against v0.8.293. A different LK stops preparation so that its
boot format can be inspected first. It patches only AVB flags in your backed-up
vbmeta. `plan.json` records the slot, layout and input paths.

Use `--profile ram` and a different output directory for the storage-disabled
image. That profile is useful for the first UART session. The expdb profile is
the one that enables persistent kernel logging.

When ready to test the chosen slot on the connected device:

```sh
bash /rabbitr1/prepared/r1-001-a/flash.sh --write
```

The script first checks the live GPT, LK and current contents against the
backups. It writes only `boot_a`, `dtbo_a` and `vbmeta_a` (or the selected `b`
equivalents), then reads them back and compares every byte. It does not write
preloader, LK, GPT, super, userdata or calibration partitions. It does not reboot
or change the active slot. Confirm that the intended slot will be booted before
leaving the connection mode. A failed boot may cause the stock A/B bootloader
to fall back to another slot; record that behavior rather than reflashing both.

## Read logs and restore

Once Linux reaches the initramfs, `r1-report` shows logger setup status.
`/sys/fs/pstore/console-pstore-blk-0` contains the previous boot's recovered
console when available. The current kernel console is written continuously
through pstore; it is not a userspace copy of `dmesg`. Setup checks the GPT label,
device number and exact 20 MiB size, and maps only the first 18 MiB.

For offline recovery, reconnect with mtkclient and dump the full partition to a
new file before booting stock firmware or another mainline attempt:

```sh
cd /rabbitr1
source scripts/env.sh
toolchains/mtkclient/bin/python src/mtkclient/mtk.py r expdb backups/r1-001/expdb-after.img
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

Restore the three selected slot partitions from the saved device backups:

```sh
bash /rabbitr1/prepared/r1-001-a/restore.sh
bash /rabbitr1/prepared/r1-001-a/restore.sh --write
```

Restore verifies the device GPT/LK, then writes and reads back the original boot,
DTBO and vbmeta. It leaves `expdb` intact for diagnosis. After copying out logs,
restoring the original shared log partition is a separate optional operation:

```sh
cd /rabbitr1
source scripts/env.sh
toolchains/mtkclient/bin/python src/mtkclient/mtk.py w expdb backups/r1-001/expdb.img
toolchains/mtkclient/bin/python src/mtkclient/mtk.py r expdb backups/r1-001/expdb-restored.img
cmp backups/r1-001/expdb.img backups/r1-001/expdb-restored.img
```

None of the device commands above have been run in this workspace; no test
device is available. Keep the original firmware archive as a separate recovery
reference. A byte-for-byte write verification is not a successful boot test.
