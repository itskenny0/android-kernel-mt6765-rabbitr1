Modeled audit-profile CI fixtures
================================

This suite tests admission of the two build-android-super.py audit profiles.
It never executes an Android image auditor, reads current Android images, runs
lpmake or claims the generated pass-shaped record is a genuine passing audit.
All generated logs, image bytes and JSON records explicitly identify themselves
as synthetic. The supplied pinned lpmake is hashed only; actual lpmake/geometry/
payload/durability tests remain in the separate test-android-super.py suite.

Producer fixture boundary
-------------------------

producer/*.py.txt contains the exact three reviewed producer source snapshots:
verify.py 58,863 bytes, provenance.py 12,021 bytes, health_checks.py 18,458 bytes
(total 89,342). They are data files, non-executable and never imported. Tests
copy their bytes into a fresh private output's producer/ directory under the
precise names required by the consumer's production hash checks. The consumer's
SOC_PRODUCER constants are never patched or replaced with synthetic hashes.
verify.py is AST-parsed to independently compare all eighteen check names.

These snapshots include historical /rabbitr1 paths inside source text. Those
paths are not consulted: no snapshot is executed. A Python audit hook rejects
execution attributed to either the data fixtures or their runtime copies, and
rejects child/tool process creation. The execution guard has a negative control
using only a synthetic code object with each data/runtime fixture filename, so no producer body
is run. Another guard detects any modeled image-content open during admission.
Production plan-only CLI paths run through the real main/argparse in-process
under these same guards; lpmake cannot be launched.

provenance.json pins each original file, its review origin and exact byte hash.
The originals were project-authored private audit helpers with no SPDX headers.
No header is inserted into these immutable snapshots. This fixture adds no
firmware, AOSP tool binary, APK or real audit/image record. Distribution licensing
follows the maintainer's project policy; the new runner is Apache-2.0 like its
existing sibling. The 89,342-byte fixture set is intentional: removing producer
bytes would require mocking the very source-integrity gate under test.

Running and scope
-----------------

From the installed workspace, after prepare-lp-tools.py::

  python3 scripts/test-android-super-audit.py \
    --lpmake /rabbitr1/toolchains/android-lp/bin/lpmake \
    --out /rabbitr1/out/android-super/ci-audit-profile-tests

The lpmake SHA defaults to the same pinned digest as test-android-super.py and
can be supplied explicitly with --lpmake-sha256. The scripts locate fixtures
relative to their repository or installed scripts directory, independent of
the checkout location and current working directory. They retain the builder's
existing /rabbitr1 and out/android-super confinement; this change does not make
production outputs arbitrary host paths. Use a fresh --out each time. Existing,
outside-tree and symlink output paths reject before creation.

The 134 cases preserve the prior 131 admission, CLI, fixture-execution,
image-open and --out controls. Three additional negatives coherently bind the
old f725 kernel, or restore one exact historical imported producer at a time,
and require the intended kernel/producer rejection before any image is opened. Current soc-cp2a-18 is the actual default. Legacy is accepted
only with explicit legacy-default-10 and carries its historical mock-Health
coverage limitation. There is no automatic fallback or claim of current release
readiness. Source snapshots, expected shapes and pins require review together
when the real producer changes.

The existing 84-case actual-lpmake suite must retain its explicit historical
profile selection from the production candidate. Both commands belong in CI.
Successful schema tests cannot replace terminal Android build records, the
executed eighteen-check image audit or its real-record compatibility check.

E98 producer rebinding
----------------------

The current SOC profile is bound to kernel
e98ffe5fcee6ce89fb754003059334ba1cf50b5c. Only the two imported helpers' kernel
constants changed; the main eighteen-check verifier is byte-identical. Fixture
provenance preserves the prior review pin and separately names the new producer
preparation handoff. This rebinding is not an executed Android image audit.

The previous f725 SOC producer is not an automatic compatibility profile. The
current default must reject its kernel/producer bindings. The separate explicit
legacy-default-10 mode keeps its existing historical scope and limitation.
