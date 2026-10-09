# Diagnostic shutdown after expdb capture

Add `r1.expdb=1 r1.log_shutdown=1` to a diagnostic initramfs boot image to
capture logs and request power-off after checking their storage readback.
The normal boot command line does not enable this mode. It is for this
initramfs only, which has no process writing `/dev/pmsg0`; do not run it as an
Android shutdown service.

The worker waits 60 seconds after it starts, then requires the expdb logger's
ready record and its bounded device mapping. It writes a checkpoint containing
the current kernel boot UUID and release to printk, unloads `pstore_blk`, and
runs `expdb-checkpoint`. The packaged kernel includes the pstore cleaner teardown
fix so delayed retries cannot access freed zones. Module unload alone does not
report whether the final flush succeeded.

The verifier requires the `r1-expdb` mapping to contain one linear target for
the first 18 MiB of a 20 MiB MMC partition named `expdb`, with no pending table
or suspension. It checks the device numbers and geometry, calls `fsync`, then
reads the 1 MiB console zone at offset 64 KiB with aligned `O_DIRECT` I/O. The
ring must have valid bounds and exactly one matching checkpoint. It rechecks
the mapping and unloaded logger before reporting success. The final 2 MiB of
expdb remain outside the mapping.

Only a successful check permits `busybox poweroff -f`. This bypasses signalling
the shell PID1 while retaining BusyBox's sync and the kernel's normal device
shutdown path. The kernel uses PSCI `SYSTEM_OFF`; actual firmware power-off
behavior still needs a device test. Charging firmware may respond to a cable
that remains connected.

This worker requires Linux to reach the initramfs and bring up eMMC. It cannot
save a checkpoint or power off a boot that stops before those stages.

Any missing logger, failed unload, failed flush, malformed ring or missing
checkpoint leaves the diagnostic shell running. Status is in
`/run/expdb-shutdown.status` and `/run/expdb-shutdown.log`. The 60-second wait is
not a hard deadline for kernel I/O or power-off callbacks.

A successful verifier establishes a storage-stack checkpoint through the
marker. It does not promise retention of every earlier line if the ring wrapped,
physical durability against faulty media, or capture of messages emitted after
pstore was stopped. Recover expdb with mtkclient and use `decode-expdb.py` to
confirm the marker and inspect the retained kernel log. A shutdown request or
USB disappearance alone does not establish that those records survived.
