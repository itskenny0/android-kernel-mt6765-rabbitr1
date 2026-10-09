# Expected eMMC identity for diagnostic commands

The patched mtkclient accepts `--expected-emmc-cid HEX32` on `gpt`, `r`, and
`w`. Supply the full expected 16-byte CID as 32 hexadecimal digits after the
command name. This is an opt-in check; ordinary discovery and commands without
it retain their existing behavior. It neither enables writes nor replaces the
diagnostic script's separate `--write` gate.

The check runs in the connected command process after DA setup or `.state`
reinitialization, including setup's possible reconnect, and before GPT/read/write
dispatch or optional key generation. It queries `get_emmc_info(display=False)`
afresh on the same DAXFlash owner used by dispatch. Cached `.state`, cached CID,
and previous eMMC objects cannot authorize the command. Only XFlash eMMC with
valid 512-byte geometry and an exact matching bytes/bytearray CID is admitted.
A missing response, query error, poisoned transport, malformed identity or CID
mismatch raises an error before dispatch and closes host resources without a
reset request. A cleanup error does not replace the admission failure.
If connection or DA configuration returns no owner before dispatcher entry,
the existing Main path can still exit zero without CID admission or a storage
command. This patch does not change that exit behavior. Required output,
full-size and immediate readback checks remain mandatory; exit status alone is
not proof of a completed operation.

This does not promise zero USB or bootstrap effects on a mismatched device:
USB discovery, preloader interaction and DA upload to RAM can precede the check.
The operator must still select the intended physical device. The CID is reported
by the connected DA, not cryptographic device attestation. Existing DA query
framing/status semantics remain unchanged; a nonzero terminal query status
returns no identity and is rejected. The parser accepts the real metadata's
opaque extension without requiring it to be empty or inventing new fields.

The current diagnostic scripts issue one partition command per process. Their
runtime integration must pass the same explicitly admitted expected CID on
**every** GPT query, original read, write and immediate readback, using the same
process that performs that command. A separate `get CID` subprocess cannot bind
a later process across reconnect. No reconnect occurs in the current guarded
command dispatch. This is a serialized-owner contract, not protection against
unreviewed code that swaps owners or reconnects after the check.

This patch provides the CLI gate. It does not yet add CID metadata to
`prepare-flash.py` or make existing prepared scripts identity-bound. Those scripts
must be regenerated with the separate reviewed runtime integration; a generic
package or old plan is not proof of that integration. Full original backups,
GPT/range checks, per-slot LK tail preservation and immediate readback remain
required. It does not change the selected slot, security state or reboot policy.

Run the focused host check against the prepared tree:

```sh
python3 scripts/test-mtkclient-cid.py \
  --source /rabbitr1/src/mtkclient-haretic \
  --out /rabbitr1/out/mtkclient-cid-tests
```

The test executes extracted actual CLI, query, guard, dispatcher and connection
method bodies with synthetic CIDs and modeled USB/bootstrap endpoints. It covers
new DA setup and `.state` reinit ordering, fresh versus cached identity, rejected
queries/fields, original-error preservation and parser rejection before Main.
It never imports target USB modules or discovers a device. Existing preparer,
transport and XFlash read suites remain separate regression checks. There is no
hardware write or media-durability claim.
