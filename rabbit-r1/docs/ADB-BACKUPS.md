# Read-only ADB backups

`scripts/receive-adb-backup.py` streams an explicitly reviewed read plan into a
private Linux-host backup directory. It reads complete physical partitions,
complete eMMC boot areas, or bounded first/last-MiB GPT windows. It has no
installer, restore, device-write, reboot, root-escalation or RPMB operation.
The default only checks the host plan, paths and space; `--execute` enables the
reviewed device reads.

The device must already expose root ADB with shell-v2. The receiver checks
`shell_v2` and uses non-PTY `adb shell -T`, preserving separate binary stdout,
stderr and remote exit status. It does not use `exec-out`, whose raw stream does
not provide those separate channels. The receiver expects a configured ADB server at 127.0.0.1:5037 and an explicit
serial. It invokes only ADB `features` and `shell`; the ADB client itself may
manage server startup if the server is absent. Configure the host ADB environment
before execution. Use a regular executable ADB path and a trusted private host
workspace. Linux, Python 3.9+ and libc `renameat2` are required.

## Supply an independently reviewed plan

No device identifier, inventory, GPT profile, actual plan or capture is shipped
with this tool. Generate and review the plan externally from the intended
device's identity, physical inventory and both physical GPT copies. For the
currently reviewed r1 layout with 47 GPT partitions, a complete system plan
covers all 46 non-userdata partitions and both readable eMMC boot areas: 48
complete ranges. Those counts are not a universal layout rule; the independently
validated device GPT and inventory are authoritative. GPT windows are separate
metadata captures. The receiver accepts smaller reviewed plans too, so it cannot
establish that a plan contains every required system partition.

Exclude the actual userdata node explicitly. The receiver rejects that node
and names containing `userdata`, but cannot infer which physical node is
userdata without the external GPT mapping. Do not substitute a partition
number, CID or serial copied from another device. No generated command or
successful backup grants permission to flash or restore.

Plan schema 1 has exactly these top-level fields:

| Field | Meaning |
| --- | --- |
| `schema` | Integer `1` |
| `serial`, `cid` | Intended device's alphanumeric serial and 32 lowercase hex CID |
| `disk` | `node`, `bytes`, `logical_sector_bytes`, `major`, `minor` |
| `excluded_nodes` | Nonempty list of actual excluded physical partition nodes, including userdata |
| `reserve_bytes` | Host free-space reserve, at least 1 GiB |
| `timeout_seconds`, `idle_seconds` | Per-process total and stdout-progress deadlines |
| `reads` | Between 1 and 128 reviewed ranges |

Each range has exactly `name`, `kind`, `node`, `bytes`, `start_sector`, `major`,
`minor`, `offset`, and `length`. Unknown or duplicate JSON keys are rejected.
Names use lowercase letters, digits, dash or underscore, up to 64 characters.
The disk is a direct `/dev/block/mmcblkN` node. Byte sizes and ranges must align
to the disk logical-sector size.

- `partition`: `/dev/block/mmcblkNpX`, full node length, offset zero.
  `start_sector` is the disk-relative partition start in **512-byte sysfs
  units**, not an offset to use when reading the partition node.
- `boot`: only that disk's `boot0` or `boot1`, full node length, offset zero,
  `start_sector: null`. These are separate address spaces. No `force_ro` change
  is made.
- `gpt`: the exact disk node, `start_sector: null`, at most 1 MiB wholly within
  the first or last 1 MiB. These captures do not themselves validate GPT.

Arbitrary whole-disk reads, partial partition/boot reads, overlapping duplicate
ranges and RPMB are rejected. Before each read, the same shell invocation
checks root UID, serial, CID, disk/node sizes and device numbers, disk sector
size, partition start where applicable, and a direct non-symlink block node.
It then runs only `dd if=...` with bounded byte offset/count and no output-device
argument. The geometry checks do not freeze or quiesce the running system.

## Check, then execute the reviewed read plan

Create a private workspace and an existing backup parent directory. Provide
absolute, non-symlink paths. For example, this command performs only host checks
and displays the generated read commands:

```sh
python3 scripts/receive-adb-backup.py \
  --plan /private/workspace/reviewed-plan.json \
  --workspace /private/workspace \
  --out /private/workspace/backups/new-backup \
  --adb /absolute/path/to/adb
```

After reviewing the exact device and plan, add `--execute` to perform those
reads. The output and its `.partial` sibling must both be absent. There is no
automatic retry, resume or overwrite. Keep host ADB/config/temp files in the
chosen private workspace as appropriate; the receiver never changes `HOME`.

## Completion and limitations

Before execution, free host space must cover all requested bytes plus the
reserve and 16 MiB metadata allowance. Transfer memory is bounded, and free
space is checked throughout. Stdout must have exactly the requested length,
both stdout/stderr pipes must reach EOF, and the process must exit zero.
Stderr is retained separately up to 64 KiB. Deadlines, host write errors,
wrong lengths, nonzero exits and excessive stderr fail the operation.

Files are created exclusively with mode `0600` in a `0700` private directory.
Each completed file is fsynced, closed and independently reread on the host to
verify its SHA256. The final manifest records the plan and tool hashes and each
transfer result. It is written after all payloads. The complete directory is
published with a no-clobber rename followed by parent-directory fsync.

An `entry-verified-private` progress line is not completion of the whole backup.
Require both successful command exit and the final complete manifest. Failures
retain partial evidence; never promote or resume it automatically. A final
parent-directory fsync failure reports failure even if the renamed directory
and complete-looking manifest are already visible. A manifest alone is not a
success or durability token. Host fsync does not establish source-media state.

These are sequential live reads, not an atomic or cold snapshot. Filesystems
and device state can change between or during reads. Host rereading verifies
the received file, not a second device read. A complete manifest proves neither
restore correctness nor install/relock safety. Keep backups and manifests
private: system partitions can contain device identities and other sensitive
data. The tool is not a sandbox against a hostile concurrent same-UID writer.

## Fake-process regression tests

```sh
python3 scripts/test-adb-backup.py --out /rabbitr1/out/adb-backup-tests
```

Use a new result directory. The 34 controls run the actual receiver with fake
ADB and `dd` executables and execute its generated shell guards with mocked
utilities. They require no ADB installation, network, USB or device access.
Fixtures contain only synthetic identifiers and data. Coverage includes binary
receive/hash, separate stderr, missing shell-v2, identity/range failures before
`dd`, short/extra streams, remote nonzero exit, timeout cleanup, host write/
fsync/reread/publication failures, exclusions, paths and host-only default mode.
These tests do not establish physical transport reliability or backup/restore
readiness. Source and fixture provenance is in `tests/adb-backup/provenance.json`.
