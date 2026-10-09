#!/usr/bin/env python3
"""Compile pinned Android pstore collectors with confined host file fixtures."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile

ROOT = Path('/rabbitr1')
R1 = Path(__file__).resolve().parents[1]
PROJECTS = {
    'bootable/recovery': {'recovery-persist.cpp', 'volume_manager/PublicVolume.cpp',
                         'volume_manager/include/volume_manager/VolumeManager.h'},
    'frameworks/native': {'cmds/dumpstate/dumpstate.cpp'},
    'system/logging': {'liblog/pmsg_reader.cpp', 'liblog/pmsg_writer.cpp',
                       'liblog/include/private/android_logger.h', 'liblog/liblog.map.txt'},
}
REPOS = {'bootable/recovery': 'android_bootable_recovery',
         'frameworks/native': 'android_frameworks_native', 'system/logging': 'android_system_logging'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=ROOT / 'downloads/pstore-collection-ci')
    parser.add_argument('--pmsg-inputs', type=Path, default=ROOT / 'downloads/pmsg-ci')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/android-pstore-collection')
    parser.add_argument('--cxx', default='clang++')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('android_patches',
                                                R1 / 'scripts/apply-android-patches.py')
    patches = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patches)
    output = patches.confined(args.out)
    output.mkdir(parents=True, exist_ok=True)
    roots = {'downloads/pmsg-ci/': patches.confined(args.pmsg_inputs),
             'downloads/pstore-collection-ci/': patches.confined(args.inputs)}
    records = {r['project']: r for r in patches.load_series(R1 / 'android/patches/series.json')
               if r['project'] in PROJECTS}
    if set(records) != set(PROJECTS):
        raise ValueError('Missing pinned collector project')
    for project, paths in PROJECTS.items():
        if {item['path'] for item in records[project]['files']} != paths:
            raise ValueError('Unexpected collector patch scope: ' + project)
    lock = json.loads((R1 / 'sources.lock.json').read_text())
    verified, pins = {}, {}
    for key, pin in lock['ci_files'].items():
        prefix = next((p for p in roots if key.startswith(p)), None)
        if prefix is None:
            continue
        name = patches.relative_name(key.removeprefix(prefix))
        project = next((p for p in PROJECTS if name.startswith(p + '/')), None)
        if project:
            expected_url = ('https://raw.githubusercontent.com/LineageOS/' + REPOS[project] + '/' +
                            records[project]['base_commit'] + '/' + name.removeprefix(project + '/'))
            if pin['url'] != expected_url:
                raise ValueError('Pinned URL differs from project base: ' + name)
        elif name != 'system/core/libcutils/include/cutils/list.h':
            raise ValueError('Unexpected collector input: ' + name)
        data = patches.confined(roots[prefix] / name).read_bytes()
        if len(data) != pin['bytes'] or digest(data) != pin['sha256'] or name in verified:
            raise ValueError('Input hash/size mismatch or duplicate: ' + name)
        verified[name], pins[name] = data, pin
    for project, record in records.items():
        for item in record['files']:
            if digest(verified[project + '/' + item['path']]) != item['before_sha256']:
                raise ValueError('Input does not match original patch hash: ' + item['path'])

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    env = os.environ.copy()
    env.update(TMPDIR=str(ROOT / '.tmp'), ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',
               UBSAN_OPTIONS='halt_on_error=1')
    fixture = patches.confined(R1 / 'tests/pstore-collection')
    results = []
    with tempfile.TemporaryDirectory(prefix='collection-', dir=output) as temp:
        scratch = Path(temp)
        original, patched = scratch / 'original', scratch / 'patched'
        for name, data in verified.items():
            dest = original / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        shutil.copytree(original, patched)
        for project, record in records.items():
            dest = patched / project
            patches.git(dest, 'init', '-q')
            patches.check_patch_paths(dest, record)
            patches.git(dest, 'apply', '--check', '-', data=record['data'])
            patches.git(dest, 'apply', '-', data=record['data'])
            for item in record['files']:
                if digest((dest / item['path']).read_bytes()) != item['after_sha256']:
                    raise ValueError('Unexpected patched file: ' + project + '/' + item['path'])

        flags = [args.cxx, '-std=c++20', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                 '-fsanitize=address,undefined', '-fno-omit-frame-pointer']

        def define(name, value):
            return '-D' + name + '=' + json.dumps(str(value))

        def execute(name, command, expected):
            binary = scratch / name
            command = [*command, '-o', str(binary)]
            compiled = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
            (output / (name + '-compile.log')).write_text(compiled.stdout + compiled.stderr)
            compiled.check_returncode()
            result = subprocess.run([str(binary)], env=env, capture_output=True, text=True, timeout=30)
            text = result.stdout + result.stderr
            (output / (name + '.log')).write_text(text)
            result.check_returncode()
            if expected not in text:
                raise ValueError('Missing expected scenario result: ' + name)
            print(text.strip(), flush=True)
            results.append({'name': name, 'command': command, 'returncode': result.returncode,
                            'output': text})

        for label, tree in [('original', original), ('patched', patched)]:
            extra = ['-DORIGINAL'] if label == 'original' else []
            execute('collection-' + label,
                    [*flags, '-pthread', '-I' + str(fixture / 'include'), *extra,
                     define('R1_TEST_DIRECTORY', scratch / ('files-' + label)),
                     define('R1_RECOVERY_SOURCE', tree / 'bootable/recovery/recovery-persist.cpp'),
                     str(fixture / 'collection.cpp'),
                     '-Wl,--wrap=_ZNSt6chrono3_V212steady_clock3nowEv', '-Wl,--wrap=nanosleep'],
                    '11 cases' if label == 'original' else '28 real-file cases')
            source = (tree / 'frameworks/native/cmds/dumpstate/dumpstate.cpp').read_text()
            body = re.search(r'static void DoKmsg\(\) \{.*?\n\}', source, re.S)
            if body is None:
                raise ValueError('Missing actual DoKmsg body')
            fragment = '\n'.join(re.findall(r'^#define \w*PSTORE_LAST_KMSG .+$', source, re.M))
            fragment += '\n' + body[0] + '\n'
            include = scratch / ('dumpstate-' + label + '.inc')
            include.write_text(fragment)
            execute('dumpstate-' + label,
                    [*flags, *extra, define('R1_TEST_DIRECTORY', scratch / ('dumpstate-files-' + label)),
                     define('R1_DUMPSTATE_FUNCTION', include), str(fixture / 'dumpstate.cpp')],
                    'original DoKmsg ignores' if label == 'original' else '10 production DoKmsg cases')
            results[-1]['extracted_body_sha256'] = digest(fragment.encode())

        library = patched / 'system/logging/liblog'
        execute('reader',
                [*flags, '-Wno-missing-field-initializers', '-Wno-vla',
                 '-I' + str(library / 'include'), '-I' + str(library),
                 '-I' + str(original / 'system/core/libcutils/include'),
                 define('R1_TEST_DIRECTORY', scratch / 'reader-files'),
                 define('R1_PMSG_READER_SOURCE', library / 'pmsg_reader.cpp'),
                 str(fixture / 'reader.cpp'), '-Wl,--wrap=open,--wrap=close,--wrap=read,--wrap=lseek'],
                '26 actual liblog reader cases')

    report = {'projects': {p: {'base_commit': r['base_commit'], 'patch': r['patch'],
                               'patch_sha256': r['sha256']} for p, r in records.items()},
              'inputs': pins, 'results': results,
              'fixtures': {str(p.relative_to(fixture)): digest(p.read_bytes())
                           for p in sorted(fixture.rglob('*')) if p.is_file()},
              'sanitizers': ['address', 'undefined'], 'hardware_tested': False,
              'limitations': 'Host file fixtures and boundary mocks; no Android ABI, runtime policy, boot or persistence validation. Backend selection does not pin same-backend inode identity.'}
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: pinned production collectors; no active Android checkout or device I/O')


if __name__ == '__main__':
    main()
