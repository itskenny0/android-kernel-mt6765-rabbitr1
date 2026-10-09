Android pstore collection checks
================================

Run ``python3 scripts/test-android-pstore-collection.py`` after the pinned CI
inputs have been fetched. The runner applies the actual recovery, dumpstate and
liblog patches in a temporary directory. It needs no Android checkout, device,
mount or driver. All fixture files stay under /rabbitr1.

The checks compile the complete recovery-persist source, the exact DoKmsg body,
and the real liblog reader. Ordinary file fixtures and syscall boundaries cover
identical-file comparison across 16 KiB chunks, changed-console rotation,
console-only boots, bounded late discovery, backend preference, and errors.
A modeled monotonic clock avoids real waits. Original-source controls reproduce
the comparison EOF bug and missing blk console. Address/undefined sanitizers run
on all cases. Existing automatic-reader/writer regressions remain in
``test-android-pmsg.py`` for both user and userdebug configurations.

The explicit private reader selects one fixed backend and never falls back.
It distinguishes clean EOF from read EAGAIN and validates skipped log-id/pid/time
records with bounded reads. Errors after valid fragments must produce no save
callbacks. A read following an earlier truncated header verifies preread reset.
The existing reader ABI and automatic fallback behavior are retained.

The collector keeps ramoops preference and waits at most 30 seconds in its
existing exec_background process for a late pstore_blk backend. No-record boots
therefore wait in the background, without an init barrier. It does not erase
pstore records. A selected pmsg read failure stops collection before console
copying and leaves the source records available for manual retrieval.

Backend selection is not an atomic snapshot: unlink/recreate within the chosen
backend can change the opened inode, and pmsg plus console writes are not a
transaction. Existing static preread state still requires serialized pmsg reads.
Source DAC/SELinux review does not establish runtime enforcing behavior; current
boot arguments request global permissive mode. Android symbol-map/linking,
packaging, device persistence and real boot remain separate validation steps.
