# Offline LK slot preparation

`scripts/lk_slots.py` prepares complete `lk_a` and `lk_b` byte images separately.
Each original must be exactly 1,048,576 bytes. It replaces the first 864,000 bytes
with the reviewed patched prefix and preserves that original's remaining
184,576 bytes. The slots may have different tails and different output hashes.

This is an offline preparation tool. It produces no flash/restore commands and
is not integrated into `prepare-flash.py` or the diagnostic package. The existing
diagnostic helper still requires a selected stock-prefix backup.

## Accepted prefixes and originals

The pure API pins these exact SHA-256 values:

| Prefix | SHA-256 |
| --- | --- |
| RabbitOS v0.8.293 stock | `534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e` |
| Reviewed patched prefix | `e4299344da5d5dd1a202b4964de764c43557f97203799e77bab55ac962973de0` |

“Current patched” refers to this one hash, not any later file in `dist/lk`.
A new build that changes the prefix requires a reviewed pin update. Neither a
caller-supplied hash nor a build-record flag can broaden the admitted versions.

Each original may contain the known stock or patched prefix. One complete slot
that is uniformly zero or uniformly `ff` is also accepted if the other slot has
a recognized prefix. Both blank slots, a blank prefix with a nonblank tail,
unknown versions, partial patches and incomplete originals reject. Accepting a
blank original says nothing about whether the original pair boots or whether
restoring it is safe for the hardware's current selector.

## File-only command

Supply saved complete originals and the generated prefix as regular files under
`/rabbitr1`. The output directory must be new and its parent must already exist.
For example:

```sh
python3 scripts/prepare-lk-slots.py \
  --lk-a /rabbitr1/backups/example/lk_a.img \
  --lk-b /rabbitr1/backups/example/lk_b.img \
  --patched-lk /rabbitr1/dist/lk/lk.bin \
  --out /rabbitr1/out/lk-pair-example
```

The command writes only `lk_a.img`, `lk_b.img` and `preparation.json`. It rejects
symlinked/outside paths, nonregular or wrong-size inputs, unsupported bytes and
preexisting output. It checks the input observations again and reads back both
output files before writing the record. Files are created without overwriting
existing entries. A failure can leave a new partial directory for inspection;
choose a new output path after resolving it. There is no group-atomicity or
physical media-durability guarantee. `preparation.json` can exist after a late
file or directory sync failure; its presence is neither a success token nor
installer admission. Callers must check the exit status and independently
validate any artifacts they later consume.

The record names each original's complete SHA-256/profile and each resulting
slot image's full SHA-256/length, plus both reviewed prefix hashes. Identical
output bytes do not prove that the same originals were supplied. Before any
future installer write, both original hashes must match the complete named cold
backup containers in that device/GPT/package transaction. A file observation,
this record, or an earlier hash is not such a binding. The eventual consumer must
also rehash output files; they can change after this command returns.

Device identity, cold-state acquisition, EXT_CSD selection, unlocked state,
backup durability, RPMB behavior and the complete matching Android/AVB package
remain outside this tool. The patched LK's existing relock refusal covers only
that reviewed handler; it does not establish global relock safety. Complete
verified stock restoration remains necessary before considering relock.

## API and tests

`prepare(original_a, original_b, patched)` accepts exact immutable `bytes` and
returns `PreparedSlots(images, originals, profiles)` with ordered `lk_a`/`lk_b`
tuples. `validate()` rechecks those bytes against the supplied complete originals
and rejects malformed tuple/string types before comparing their values. The
module performs no filesystem or device I/O.

The portable standard-library suite accepts explicit input/output paths:

```sh
python3 scripts/test-lk-slots.py \
  --stock firmware/stock-v0.8.293/lk.img \
  --patched dist/lk/lk.bin --build-record dist/lk/build.json \
  --out out/lk-slots-tests
```

CI runs this after `build-lk.py`, when its actual pinned prefixes are available.
The suite uses those real prefix bytes and synthetic full-slot tails. It covers
the allowed/forbidden original pairs, per-slot tails, stale bindings, strict
nested types, CLI file/output errors and input/output changes. It does not use
alternate test-only prefix pins or distribute firmware binary fixtures.

The pure module retains the reviewed revision-2 source bytes (SHA-256
`dc02ee3f496518f3334784789fc318f7d7650a7012f1e4a04633aaad837a7104`). Its private
independent review passed 61 owner controls plus 35 additional controls and
resolved the original equality-overriding binding-type gap. This provenance is
software evidence, not physical-device validation.
