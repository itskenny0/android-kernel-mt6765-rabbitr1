# mtkclient transport

haretic carries a host-side patch for mtkclient v2.1.4.1. It stops guarded USB
bulk operations after an uncertain transfer and propagates those failures
through XFlash reads and writes. It has been tested with scripted endpoints and
actual extracted upstream methods; it has not been tested on an r1.

## Prepare and check the source

After fetching the pinned source archives, prepare the separate patched tree:

```sh
cd /rabbitr1
source scripts/env.sh
python3 scripts/prepare-mtkclient.py
python3 scripts/prepare-mtkclient.py --check
```

Preparation verifies the archive and patch, applies the patch in a private
directory, checks every resulting file and publishes to
`/rabbitr1/src/mtkclient-haretic` only if that destination is absent. It preserves
`/rabbitr1/src/mtkclient`. Checking rejects altered, missing or additional files
and symlinks. Neither operation imports USB, installs dependencies or accesses a
device. An existing destination is never repaired or replaced automatically.

Keep `PYTHONDONTWRITEBYTECODE=1` when using this tree, as set by `scripts/env.sh`;
untracked bytecode fails its strict source check. The source check does not
validate the interpreter or installed dependencies, or prevent later changes
between checking and execution.

New diagnostic packages bundle `prepare-mtkclient.py`,
`mtkclient-transport.json` and `mtkclient-transport.patch`. Their manifest pins
those files, the retained upstream archive and the separate patched tree.
The flash preparer rejects missing or stale transport metadata, including a
destination pointing to the original source tree. Copied checker inputs must
match the admitted package pins before either flash script is produced.

Generated flash and restore scripts check the three bundled file hashes and run
the bundled checker before **every** mtkclient process: GPT, preflight reads,
writes and immediate readbacks. A failed check prevents that process from
starting. The checker requires the retained local archive and rechecks the
complete derived source tree each time; it performs no download. The scripts
select `/rabbitr1/src/mtkclient-haretic` and preserve no-bytecode mode.

Existing diagnostic archives and previously generated scripts keep their
recorded transport. Preparing the patched tree does not migrate them, and the
new helper has no fallback for old package metadata. An explicit host-tool
refresh can preserve older validated payloads, but must retain their original
kernel provenance and revalidate against an independently pinned original
manifest. See [FLASHING.md](FLASHING.md) for package use; do not substitute
current kernel outputs into a historical package without rebuilding it.

## Changes

The original write loop could stall on zero progress, retry an unknown partial
transfer, or accept an invalid count. Fast reads used an unlimited USB timeout
and could reuse a buffer larger than the final remainder. The patched methods
validate counts, advance only by acknowledged bytes, use finite timeouts and
reject later guarded operations after a failure.

The patch also fixes two misleading results. A failed final read acknowledgement
could return a complete-looking buffer. A write failure could be logged and
converted to `False`, which the command-line handlers could ignore before
exiting successfully. Latched transport failures now escape those paths so a
shell using `set -e` stops before its next command.

XFlash reads also require the exact byte count selected by the existing storage
resolver. Malformed headers, oversized frames, failed commands or acknowledgements,
and a nonzero terminal status raise an error instead of returning a partial dump.
The terminal frame must contain exactly four bytes with raw status zero. A read
failure now reaches the command-line caller as a nonzero exit status.

File reads stream into a private temporary file beside the destination. Exact
write counts, final size and successful close are checked before replacement.
An existing dump remains intact on failure. Output paths must have ordinary,
non-symlink directory parents and an absent or regular, single-link destination;
paths containing `..` are rejected. Successful dumps have mode `0600`; replacing
a dump changes its inode and does not preserve its previous ownership or metadata.
This is not protection against concurrent hostile path changes or power loss.

A local write failure before terminal completion also stops further guarded
traffic on that instance. Host errors after a validated terminal still fail the
command, but do not mark the transport as desynchronized. Cleanup removes the
owned temporary file when possible and issues no device command.

Intentional zero-length writes, explicit short reads (`maxtimeout=-1`) and
healthy reconnects remain supported. A failed instance cannot reconnect; a new
session is required. Closing that instance releases host resources without
requesting a device reset. It cannot undo device-side effects.

## Limits

The separate software budgets are a 1 MiB endpoint chunk limit, 65,536 endpoint
calls, and 120 seconds of observed completion time per logical transfer. Each
call receives a positive timeout capped by the existing 1,000 ms default.
`maxtimeout` remains a legacy read-mode selector; its nonnegative values are not
reinterpreted as milliseconds. These budgets have not been measured on the r1.
A slow valid operation can fail, and a backend that ignores its timeout can
still block.

The guard assumes one serialized caller. Raw control transfers, discovery,
exploits and direct endpoint access are outside its scope. Handshake entry and
the old `usbxmlread` method reject an existing failure; their own raw transfer
logic remains unchanged. The XFlash read checks cover its data count, magic,
acknowledgements and terminal status. Header datatypes and four-byte data/status
ambiguity remain unresolved: data frames of four bytes or less are rejected.
The old 1 MiB fallback for an unavailable packet-size query is removed. That
query cannot distinguish an unsupported command from other failures, so either
case now stops the read. This can reject a previously usable DA; physical
compatibility has not been tested. Other DA framing/status paths, optional-command
semantics, partition selection and media durability remain separate work.

This patch does not establish a complete firmware installation procedure.
The diagnostic package and a full Android image set need their own validation.
Do not relock the bootloader while patched LK remains in either slot; restore
the complete verified stock package first.

## Evidence

The patch applies to the pinned official archive and changes only `usblib.py`,
`Port.py` and `xflash_lib.py`. Tests execute actual selected methods without
importing USB or constructing a backend. The initial review passed 77 owner
controls and 12 independent controls, including large reads, reconnects,
intentional zero-length packets, partial progress and failure cleanup.

A separate test executed the extracted write/command-handler/exit chain. The
old path exited zero and allowed the next shell process; the corrected path
exited nonzero and stopped it. Device initialization and the failure trigger
were fixtures, so this is a host-side propagation check, not a device test.

The PyUSB callback tests use official 1.3.1 source. This does not pin every
user's installed USB backend or establish hardware compatibility.

The read correction passed 61 actual-method controls and six independent
controls, including incomplete frames, file errors, destination preservation and
cleanup. Its extracted read/command-handler/exit chain exits nonzero and stops
the next shell process. The existing 77 bulk controls also pass with the combined
patch. Query/storage setup remains modeled in the read tests; they do not prove
the DA's complete protocol or physical read behavior.

The diagnostic migration retains 145 transfer/preflight/readback cases and adds
checker-failure, bundled-pin, metadata and copy-binding controls. Recording
checker/device fixtures prove process ordering; separate tests exercise the
actual offline source preparer. The migration review also passed 23 independent
controls, including changing or removing bundled inputs after the GPT read.
These checks preserve the diagnostic package's scope; they do not establish a
complete Android installation procedure or physical flash safety.
