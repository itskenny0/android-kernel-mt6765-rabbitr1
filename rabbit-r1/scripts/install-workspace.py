#!/usr/bin/env python3
"""Copy the fork's tracked r1 tooling into its fixed /rabbitr1 workspace."""
import argparse
from pathlib import Path
import shutil

ROOT = Path('/rabbitr1')
source = Path(__file__).resolve().parents[1]
if source != ROOT/'src/mainline/rabbit-r1':
    raise SystemExit('Run /rabbitr1/src/mainline/rabbit-r1/scripts/install-workspace.py')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--update', action='store_true', help='replace differing workspace tooling')
args = parser.parse_args()
files = sorted(p for p in source.rglob('*') if p.is_file())
for path in files:
    target = ROOT/path.relative_to(source)
    if not target.resolve().is_relative_to(ROOT):
        raise SystemExit('Destination escapes /rabbitr1')
    if target.exists() and target.read_bytes() != path.read_bytes() and not args.update:
        raise SystemExit(f'Preserving {target}; inspect it before using --update')
for path in files:
    target = ROOT/path.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
print(f'Installed {len(files)} tooling files in /rabbitr1; sources and artifacts preserved')
