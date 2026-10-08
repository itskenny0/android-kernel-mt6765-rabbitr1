#!/usr/bin/env python3
"""Install the r1 Android product and verified kernel inputs under /rabbitr1."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path('/rabbitr1')
SOURCE = ROOT / 'src/mainline/rabbit-r1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
                                                SOURCE / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, default=ROOT / 'src/android')
    args = parser.parse_args()
    tree = args.tree.resolve()
    if tree == ROOT or not tree.is_relative_to(ROOT):
        raise SystemExit('Use a dedicated Android tree under /rabbitr1')
    if not (tree / '.repo/manifest.xml').is_file() or not (tree / 'build/envsetup.sh').is_file():
        raise SystemExit('Initialize and sync the Android build tree first')

    files = {}
    for source, target in [('device', 'device/rabbit/r1'),
                           ('charging', 'device/rabbit/r1/charging'),
                           ('local_manifests', '.repo/local_manifests')]:
        directory = SOURCE / 'android' / source
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                files[str(Path(target) / path.relative_to(directory))] = path.read_bytes()

    dist = ROOT / 'dist/mainline'
    record = json.loads((dist / 'build.json').read_text())
    for name in ['Image.gz', 'Image', 'config', 'mt6765-rabbit-r1.dtb']:
        if digest((dist / name).read_bytes()) != record['artifacts'][name]['sha256']:
            raise SystemExit('Kernel artifact hash mismatch: ' + name)
    package = load_script('package-mtkclient')
    validate = load_script('validate-kernel')
    validate.check_mainline(dist / 'mt6765-rabbit-r1.dtb')
    prebuilt = 'device/rabbit/r1/prebuilt/'
    files[prebuilt + 'Image.gz'] = (dist / 'Image.gz').read_bytes()
    files[prebuilt + 'kernel-build.json'] = (dist / 'build.json').read_bytes()
    for name, sha in record['modules'].items():
        source = (ROOT / 'out/mainline' / name).resolve()
        if not source.is_relative_to(ROOT / 'out/mainline') or source.suffix != '.ko':
            raise SystemExit('Invalid module path in kernel record')
        data = source.read_bytes()
        target = prebuilt + source.name
        if digest(data) != sha or target in files:
            raise SystemExit('Module hash mismatch or duplicate basename: ' + name)
        files[target] = data

    # Android requires eMMC. Preserve every other bring-up gate and property.
    with tempfile.TemporaryDirectory(dir=ROOT / '.tmp', prefix='r1-android-dtb-') as tmp:
        dtb = Path(tmp) / 'r1.dtb'
        dtb.write_bytes((dist / 'mt6765-rabbit-r1.dtb').read_bytes())
        subprocess.run(['fdtput', '-t', 's', str(dtb), '/soc/mmc@11230000',
                        'status', 'okay'], check=True)
        expected = validate.fdt_nodes((dist / 'mt6765-rabbit-r1.dtb').read_bytes())
        expected['/soc/mmc@11230000']['status'] = b'okay\0'
        if validate.fdt_nodes(dtb.read_bytes()) != expected:
            raise SystemExit('Unexpected change to the Android DTB')
        files[prebuilt + 'dtb/table.dtb'] = package.dt_table(dtb.read_bytes())

    # LK applies this to its private vendor tree; a mainline overlay is unsuitable.
    firmware = ROOT / 'firmware/stock-v0.8.293/dtbo.img'
    stock_dtbo = firmware.read_bytes()
    manifest = json.loads((ROOT / 'dist/mtkclient/manifest.json').read_text())
    if digest(stock_dtbo) != manifest['stock_dtbo_sha256']:
        raise SystemExit('Stock DTBO differs from the checked diagnostic package')
    files[prebuilt + 'dtbo.img'] = stock_dtbo

    stamp = tree / '.r1-install.json'
    if stamp.is_symlink():
        raise SystemExit('Installation record must not be a symlink')
    previous = json.loads(stamp.read_text()) if stamp.exists() else {'files': {}}
    for name, data in files.items():
        dest = tree / name
        if not dest.resolve().is_relative_to(tree) or dest.is_symlink():
            raise SystemExit('Destination escapes Android tree or is a symlink: ' + name)
        if dest.exists():
            old = dest.read_bytes()
            if old != data and digest(old) != previous['files'].get(name):
                raise SystemExit('Preserving local changes: ' + str(dest))
    for name in previous['files'].keys() - files.keys():
        raise SystemExit('Obsolete managed file needs review before removal: ' + name)
    # Validate the entire installation before replacing any managed file.
    for name, data in files.items():
        dest = tree / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    stamp.write_text(json.dumps({
        'format': 1, 'kernel_source_commit': record['source_commit'],
        'hardware_tested': False,
        'files': {name: digest(data) for name, data in sorted(files.items())},
    }, indent=2) + '\n')
    print(f'Installed {len(files)} files into {tree}')
    print('Select: lunch lineage_r1 trunk_staging userdebug')
    print('eMMC enabled; hardware validation outstanding')


if __name__ == '__main__':
    main()
