#!/usr/bin/env python3
"""Install the r1 charging feature and connect an existing Android product/board."""
import argparse
from pathlib import Path
import shutil

ROOT = Path('/rabbitr1')
SOURCE = ROOT / 'src/mainline/rabbit-r1/android/charging'
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--tree', type=Path, required=True)
p.add_argument('--product', type=Path, default=Path('device/rabbit/r1/device.mk'))
p.add_argument('--board', type=Path, default=Path('device/rabbit/r1/BoardConfig.mk'))
args = p.parse_args()
tree = args.tree.resolve()
def within(path):
    path = path.resolve()
    if not path.is_relative_to(tree) or not path.is_relative_to(ROOT):
        raise SystemExit('Every destination must stay in the Android tree under /rabbitr1')
    return path
if tree == ROOT or not tree.is_relative_to(ROOT):
    raise SystemExit('Use a dedicated Android tree under /rabbitr1')
product, board = [within(tree / name) for name in (args.product, args.board)]
if product == board or not product.is_file() or not board.is_file():
    raise SystemExit('The existing r1 product makefile and BoardConfig.mk are required')
destination = within(tree / 'device/rabbit/r1/charging')
files = [(source, within(destination / source.relative_to(SOURCE)))
         for source in SOURCE.rglob('*') if source.is_file()]
# Validate every destination before writing any file.
for source, dest in files:
    if dest.exists() and dest.read_bytes() != source.read_bytes():
        raise SystemExit(f'Local changes at {dest}; review them before replacing the feature')
hooks = [(product, '$(call inherit-product, device/rabbit/r1/charging/product.mk)'),
         (board, 'include device/rabbit/r1/charging/board.mk')]
for source, dest in files:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
for path, hook in hooks:
    text = path.read_text()
    if hook not in text.splitlines():
        path.write_text(text.rstrip() + '\n\n' + hook + '\n')
print(f'Installed {len(files)} charging files and connected {product} and {board}')
print('In the configured LineageOS 24 tree, build: m R1ChargingSettings r1-charging selinux_policy')
