# GitHub Actions

The [Rabbit r1 workflow](../../.github/workflows/rabbit-r1.yml) runs on pushes to
`rabbit-r1/**` and `mt6765-devel`, on pull requests, and on manual dispatch.
It builds the exact event commit, including GitHub's test merge commit for a PR.
An updated branch cancels an older run for that branch.

The Ubuntu 24.04 runner puts the checkout, downloads, caches and build outputs
under `/rabbitr1`. Build tools are installed on the disposable runner. No local
host packages or device partitions are changed by this workflow.

The workflow:

1. Installs the tracked workspace tools and fetches pinned build inputs.
2. Checks the patch series, MT6357 mappings, bounded expdb mapper and log decoder.
3. Builds the AArch64 kernel, device tree, modules and static diagnostic initramfs.
4. Inspects stock metadata, builds patched LK and the LineageOS splash with
   pinned mtklkzap, and emulates the Linux DT handoff.
5. Builds both mtkclient boot profiles.
6. Checks boot headers, DT tables, module identity, target shell syntax, GPT and
   backup validation, AVB flags, checksums and repeat packaging.

`fetch-sources.py --profile ci` fetches mkbootimg, BusyBox, the stock firmware
archive, mtklkzap, the mtklogo binary and a checksummed MT6357 vendor header. It leaves the checked-out kernel
unchanged. The patch base is fetched separately for the patch consistency test.
The ordinary fetch command still provisions the complete development workspace.

Source archives are cached by the lockfile hash and verified on every run.
Build outputs are not cached. The cache and artifact actions are pinned to full
commit IDs. The workflow uses a read-only repository token and no stored secrets;
the public source checkout itself needs no credentials.

## Artifacts

Successful runs upload `rabbit-r1-mainline-<commit>` with the kernel, DTB,
modules, configuration, symbols, build record, initramfs, patched LK, logo,
splash preview, mtkclient ZIP and
checksums. `rabbit-r1-logs-<commit>` is uploaded even when a later step fails.
Both artifacts are retained for 14 days. Download them from the run's Actions
page and follow [FLASHING.md](FLASHING.md) before using a boot image.

The [first hosted run](https://github.com/itskenny0/android-kernel-mt6765-rabbitr1/actions/runs/37671533107)
passed in 17m55s at `98375f81dbc0b060aa04b8480868833b96779311`. Both uploaded
artifacts were downloaded and their file checksums verified. The record is in
[ci-first-run.json](research/ci-first-run.json). That historical artifact predates the [LK/DTBO fix](LK.md); use a run
that includes the LK build and emulation steps for the corrected package.

A green run means the build and listed offline checks passed. It does not
establish a working r1 boot, charging, eMMC persistence or panic recovery. The
known full DT-schema failures remain documented in [VALIDATION.md](VALIDATION.md);
CI currently gates the compiled DT's targeted consistency checks.

Manual dispatch appears in the GitHub UI once this workflow is on the default
branch. Pushes to `rabbit-r1/bringup` trigger it without changing the default
branch.
