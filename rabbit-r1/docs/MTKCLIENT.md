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

Existing diagnostic archives and previously generated scripts still use their
recorded transport. Preparing this tree does not migrate those artifacts. A
package must explicitly select and check the patched tree before it can claim
these transport changes.

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
logic remains unchanged. Ordinary DA framing and status errors, optional-command
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
