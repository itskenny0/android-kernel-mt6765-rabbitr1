#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise actual AOSP product inheritance in an isolated GNU Make fixture.

Uses pinned source fixtures by default; --android-tree checks the live source
checkout. Never invokes Soong/Kati/Ninja or an Android build target. Non-Health
inherited products are empty fixture leaves; the real node_fns, inherit-product macro, r1 templates, common options and Health package
selectors determine ordering and package selection.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
OLD = '$(call inherit-product, device/rabbit/r1/health/product.mk)'
NEW = 'include device/rabbit/r1/health/product.mk'
EXPECTED = {
    'lineage_r1': {'android.hardware.health-service.example',
                   'android.hardware.health-service.example_recovery'},
    'lineage_r1_soc': {'android.hardware.health-service.r1',
                       'android.hardware.health-service.example_recovery',
                       'r1-health-initial-ready'},
}
MOCK = {'com.google.cf.health', 'android.hardware.health-service.cuttlefish_recovery'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--android-tree', type=Path,
                        default=SOURCE/'tests/health/product-selection')
    parser.add_argument('--output', type=Path,
                        default=Path('/rabbitr1/out/health-product-selection-tests'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.android_tree/'sources.json').is_file():
        fixtures = json.loads((args.android_tree/'sources.json').read_text())
        for name, expected in fixtures['files'].items():
            if digest(args.android_tree/name) != expected['fixture_sha256']:
                raise ValueError('Pinned product-inheritance fixture changed: '+name)
    core = args.android_tree / 'build/make/core'
    common = args.android_tree / 'device/mainline/common'
    inputs = [core/'node_fns.mk', core/'product.mk', common/'optional/options.mk',
              common/'optional/health-hal_cuttlefish/product.mk',
              common/'optional/health-hal_default-aidl/product.mk']
    inputs += [SOURCE/'android/device'/n for n in
               ('device.mk', 'lineage_r1.mk', 'lineage_r1_soc.mk', 'health/product.mk')]
    pins = {str(p): digest(p) for p in inputs}
    match = re.search(r'^define inherit-product\n.*?^endef\n',
                      (core/'product.mk').read_text(), flags=re.M | re.S)
    if not match:
        raise ValueError('Missing actual AOSP inherit-product macro')
    original = (SOURCE/'android/device/device.mk').read_text()
    if original.count(NEW) != 1 or OLD in original:
        raise ValueError('r1 Health selection must execute before deferred common inheritance')
    cases = []
    with tempfile.TemporaryDirectory(prefix='health-inheritance-', dir=args.output) as name:
        tree = Path(name)
        def write(path, text):
            p = tree/path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        for relative in ('lineage_r1.mk', 'lineage_r1_soc.mk', 'health/product.mk'):
            write('device/rabbit/r1/'+relative,
                  (SOURCE/'android/device'/relative).read_text())
        for relative in ('build/make/target/product/core_64_bit_only.mk',
                         'build/make/target/product/full_base.mk',
                         'vendor/lineage/config/common_full_tablet_wifionly.mk',
                         'device/rabbit/r1/charging/product.mk'):
            write(relative, '# Empty unrelated inherited leaf for this fixture.\n')
        write('node_fns.mk', (core/'node_fns.mk').read_text())
        write('inherit-product.mk', match.group())
        for relative in ('optional/options.mk',
                         'optional/health-hal_cuttlefish/product.mk',
                         'optional/health-hal_default-aidl/product.mk'):
            write('device/mainline/common/'+relative, (common/relative).read_text())
        write('device/mainline/common/mainline_common.mk',
              'MAINLINE_COMMON_PATH := device/mainline/common\n'
              'include $(MAINLINE_COMMON_PATH)/optional/options.mk\n'
              'include $(MAINLINE_COMMON_PATH)/optional/health-hal_cuttlefish/product.mk\n'
              'include $(MAINLINE_COMMON_PATH)/optional/health-hal_default-aidl/product.mk\n')
        write('check.mk',
              'SRC_TARGET_DIR := build/make/target\n'
              'include node_fns.mk\n'
              '_product_var_list := PRODUCT_PACKAGES PRODUCT_VENDOR_PROPERTIES\n'
              'include inherit-product.mk\n'
              'product_path := device/rabbit/r1/$(TARGET_PRODUCT).mk\n'
              '$(call import-nodes,PRODUCTS,$(product_path),$(_product_var_list),)\n'
              '$(info ALL_PACKAGES=$(PRODUCTS.$(product_path).PRODUCT_PACKAGES))\n'
              '$(info HAL=$(TARGET_HEALTH_HAL))\n'
              '.PHONY: all\nall:\n\t@:\n')
        for product, expected in EXPECTED.items():
            config = 'y' if product.endswith('_soc') else 'n'
            write('device/rabbit/r1/prebuilt/health-prebuilt.mk',
                  'R1_INSTALLED_HEALTH_PRODUCT := '+product+'\n'
                  'R1_PREBUILT_SOC_CONFIG := '+config+'\n')
            for variant, text in [('fixed', original),
                                  ('deferred-negative-control', original.replace(NEW, OLD))]:
                write('device/rabbit/r1/device.mk', text)
                proc = subprocess.run(['make', '--no-print-directory', '-rR', '-f',
                                       'check.mk', 'TARGET_PRODUCT='+product], cwd=tree,
                                      capture_output=True, text=True)
                (args.output/(product+'-'+variant+'.log')).write_text(proc.stdout+proc.stderr)
                if proc.returncode:
                    raise ValueError(proc.stdout+proc.stderr)
                line = next(x for x in proc.stdout.splitlines() if x.startswith('ALL_PACKAGES='))
                packages = line.partition('=')[2].split()
                health = {p for p in packages if 'health' in p}
                if variant == 'fixed':
                    assert health == expected, (product, health, expected)
                    assert not (set(packages) & MOCK)
                    assert packages.count('android.hardware.health-service.example_recovery') == 1
                else:
                    assert MOCK <= health, (product, health)
                    assert health != expected
                    if product == 'lineage_r1':
                        assert 'android.hardware.health-service.example' not in health
                cases.append({'product': product, 'variant': variant,
                              'health_packages': sorted(health),
                              'hal': next(x[4:] for x in proc.stdout.splitlines() if x.startswith('HAL=')),
                              'passed': True})
    assert pins == {str(p): digest(p) for p in inputs}, 'Input changed during test'
    record = {'status': 'pass', 'cases': cases, 'source_sha256': pins,
              'scope': 'Actual node_fns/inherit-product and Health selectors; unrelated products stubbed; no Android graph/build/device execution'}
    (args.output/'result.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
