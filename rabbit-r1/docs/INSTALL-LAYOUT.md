# Offline installer layout inspection

`scripts/check-install-layout.py` checks saved GPT extents and pinned storage observations. Its result is **offline layout consistency**. It does not connect to a device or authorize an installation.

The reusable `scripts/r1_gpt.py` parser defaults to `disk_guid_policy="nonzero"`. Selecting `"zero"` explicitly requires an all-zero disk GUID in **both** headers. Neither profile accepts a mixture or falls back to the other. This accommodates the observed r1 GPT format without treating its disk GUID as hardware identity. Every used partition must still have a nonzero, unique partition GUID.

Both profiles retain header and array CRC checks, primary/backup metadata and array equality, physical bounds derived from the separately supplied capacity, nonoverlapping partitions, unique case-insensitive names and bounded GPT sizes. The inspector also requires the supported r1 install partition names, sizes and attributes. No GPT field is rewritten.

## Inputs and usage

Supply four exact binary extents: each 512-byte header and each complete entry array rounded up to a 512-byte sector. Read the primary and backup extents independently from their physical locations. A filename or tool export alone does not prove how bytes were acquired. The maximum supported array is 4096 entries of 128 bytes. The parser rejects other header revisions, strides and logical sector sizes.

Supply two normalized JSON observations: the saved observation associated with the GPT acquisition and an independently reviewed expected observation. Each must have exactly these fields:

```json
{
  "schema": 1,
  "chip": {
    "hw_code": 1894,
    "hw_sub_code": 1,
    "hw_version": 2,
    "sw_version": 0,
    "chip_evolution": 0
  },
  "cid": "0123456789abcdef0123456789abcdef",
  "emmc_type": 1,
  "sector_bytes": 512,
  "sizes": {
    "boot1": 4194304,
    "boot2": 4194304,
    "rpmb": 16777216,
    "gp1": 0,
    "gp2": 0,
    "gp3": 0,
    "gp4": 0,
    "user": 34359738368
  },
  "fwver": 0
}
```

These values are synthetic examples. `cid` must be 32 lowercase hexadecimal digits, neither all zero nor all `f`. The `rpmb` value is metadata only; the helper never reads a storage device. Normalize richer acquisition records deliberately, retaining the original records separately. Supply each observation's reviewed SHA-256 and the independently observed user-area capacity; do not infer capacity from the GPT.

For files already saved under `/rabbitr1/out/layout-inputs`, run:

```sh
python3 /rabbitr1/scripts/check-install-layout.py \
  --primary-header /rabbitr1/out/layout-inputs/primary-header.bin \
  --primary-array /rabbitr1/out/layout-inputs/primary-array.bin \
  --backup-header /rabbitr1/out/layout-inputs/backup-header.bin \
  --backup-array /rabbitr1/out/layout-inputs/backup-array.bin \
  --observation /rabbitr1/out/layout-inputs/observed.json \
  --observation-sha256 "$OBSERVED_SHA256" \
  --expected-observation /rabbitr1/out/layout-inputs/expected.json \
  --expected-observation-sha256 "$EXPECTED_SHA256" \
  --user-bytes "$USER_BYTES" \
  --disk-guid-policy zero \
  --out /rabbitr1/out/layout-inputs/layout.json
```

Omitting `--disk-guid-policy` retains the nonzero default. Output must not already exist. The report binds the policy, pinned parser source, inspector source, all four extent hashes, both observation hashes, capacity and parsed pair fingerprint. The inspector verifies its neighboring parser against a reviewed source hash **before executing it**; a parser revision requires a corresponding reviewed pin update.

## Limits and integration

The helper checks bounded regular files under `/rabbitr1`, rejects static symlink paths and publishes the report with mode `0600` without overwriting an existing file. It assumes a trusted workspace; these checks do not claim containment against hostile concurrent directory replacement. Keep real reports private: they contain device-specific partition GUIDs and hashes.

Matching saved observations do not prove that a connected device is the expected one, that captures are fresh, or that the device was cold or unlocked. Supplied hashes bind files; they do not establish who approved those files. Do not derive the expected observation by copying an untrusted observation and treat that as identity proof.

A future physical installer must separately validate fresh identity and geometry in the same connected DA process, plus its payload, backup, LK originals and write-admission requirements. This helper does not change the frozen installer models or the backup-only GPT parser. It provides a reusable parser and a practical offline consumer; it is not a physical installer adapter.

Run the synthetic parser and CLI controls without an Android checkout or device:

```sh
python3 /rabbitr1/scripts/test-install-layout.py \
  --out /rabbitr1/out/install-layout-tests
```

The tests include repaired-CRC malformed GPTs, unequal arrays with matching CRC32, explicit/default/mixed GUID profiles, observation and source-pin failures, and output preservation. All fixture identities and partition GUIDs are synthetic.
