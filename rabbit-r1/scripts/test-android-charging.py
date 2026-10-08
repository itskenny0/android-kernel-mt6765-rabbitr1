#!/usr/bin/env python3
"""Compile Android charging sources against pinned AOSP 17 APIs and test restoration."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path('/rabbitr1')
R1 = ROOT/'src/mainline/rabbit-r1'
APP = R1/'android/charging'
OUT = ROOT/'out/android-charging'
SDK = ROOT/'toolchains/android-compile'
os.environ['TMPDIR'] = str(ROOT/'.tmp')
lock = json.loads((R1/'sources.lock.json').read_text())
for folder in ('compiled', 'generated', 'classes'):
    (OUT/folder).mkdir(parents=True, exist_ok=True)
(SDK/'include/android-base').mkdir(parents=True, exist_ok=True)
for path, pin in lock['ci_files'].items():
    name = Path(path).name
    if not (name.startswith('android-') or name == 'aapt2.base64'):
        continue
    data = (ROOT/path).read_bytes()
    assert len(data) == pin['bytes'] and hashlib.sha256(data).hexdigest() == pin['sha256'], path
    if name.startswith('android-base-'):
        dest = SDK/'include/android-base'/name.removeprefix('android-base-').removesuffix('.base64')
    elif name == 'android-jni.h.base64':
        dest = SDK/'include/jni.h'
    elif name == 'android-public.jar.base64':
        dest = SDK/'android.jar'
    elif name == 'aapt2.base64':
        dest = SDK/'aapt2'
    else:
        continue
    dest.write_bytes(base64.b64decode(data, validate=False))
(SDK/'aapt2').chmod(0o755)
def run(args):
    subprocess.run([str(x) for x in args], check=True, timeout=90)
run([SDK/'aapt2', 'compile', '--dir', APP/'app/res', '-o', OUT/'compiled'])
run([SDK/'aapt2', 'link', '-I', SDK/'android.jar', '--manifest', APP/'app/AndroidManifest.xml',
     '--min-sdk-version', '35', '--target-sdk-version', '37', '--java', OUT/'generated',
     '-o', OUT/'resources.apk', *sorted((OUT/'compiled').glob('*.flat'))])
run(['javac', '-Xlint:all', '-classpath', SDK/'android.jar', '-d', OUT/'classes',
     *sorted((OUT/'generated').rglob('*.java')), *sorted((APP/'app/src').rglob('*.java'))])
run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsyntax-only',
     '-I'+str(SDK/'include'), APP/'native/service.cpp', APP/'native/jni.cpp'])
source = (APP/'native/service.cpp').read_text()
start = source.index('static std::string Reconcile()')
end = source.index('\n}', start)+2
(OUT/'reconcile-under-test.h').write_text(source[start:end]+'\n')
run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
     '-I'+str(SDK/'include'), '-I'+str(APP/'native'), '-I'+str(OUT),
     R1/'tests/android-charging-service.cpp', '-o', OUT/'service-test'])
run([OUT/'service-test'])
manifest = ET.parse(APP/'app/AndroidManifest.xml').getroot()
a = '{http://schemas.android.com/apk/res/android}'
activity = manifest.find('application/activity')
assert activity.get(a+'permission') == 'android.permission.DEVICE_POWER'
metadata = {item.get(a+'name'):item.get(a+'value') for item in activity.findall('meta-data')}
assert metadata['com.android.settings.category'] == 'com.android.settings.category.ia.battery'
# Exercise actual installation, including idempotent makefile hooks.
tree = OUT/'integration-fixture'
if tree.exists(): shutil.rmtree(tree)
(tree/'device/rabbit/r1').mkdir(parents=True)
for name in ('device.mk', 'BoardConfig.mk'):
    (tree/'device/rabbit/r1'/name).write_text('# Existing product\n')
for _ in range(2):
    run(['python3', R1/'scripts/integrate-lineage-charging.py', '--tree', tree])
assert (tree/'device/rabbit/r1/device.mk').read_text().count('inherit-product') == 1
assert (tree/'device/rabbit/r1/BoardConfig.mk').read_text().count('include ') == 1
print('PASS: Android 17 resource/Java compilation, native API compilation and build integration')
print('Full Soong linking, SELinux policy compilation and device UI tests still require the Android product tree.')
