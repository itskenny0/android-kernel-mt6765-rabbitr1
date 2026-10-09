#!/usr/bin/env python3
"""Check pinned Android patches, or apply them with --apply, under /rabbitr1."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

ROOT = Path('/rabbitr1')
DEFAULT_SERIES = Path(__file__).resolve().parents[1] / 'android/patches/series.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative_name(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid relative path')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or '\\' in value or
            any(part in ('.', '..', '.git') for part in path.parts)):
        raise ValueError('Unsafe relative path: ' + value)
    return value


def confined(path):
    """Reject symlinks in every component, before resolving or opening a path."""
    path = Path(os.path.abspath(path))
    if path == ROOT or not path.is_relative_to(ROOT):
        raise ValueError('Path must be inside /rabbitr1: ' + str(path))
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError('Symlink path is not allowed: ' + str(part))
        if part == ROOT:
            break
    return path


def git(directory, *args, data=None):
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    env.update(GIT_CONFIG_GLOBAL=str(ROOT / '.gitconfig'),
               GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(['git', '--no-optional-locks', '--literal-pathspecs',
                             '-C', str(directory), *args], input=data,
                            capture_output=True, env=env)
    if result.returncode:
        raise ValueError('git ' + ' '.join(args) + ': ' +
                         result.stderr.decode(errors='replace').strip())
    return result.stdout


def hex_value(value, size):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(size) + '}', value):
        raise ValueError('Invalid pinned hash: ' + str(value))
    return value


def load_series(series):
    records = json.loads(series.read_text())
    if not isinstance(records, list) or not records:
        raise ValueError('Patch series must be a nonempty list')
    projects = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
                'project', 'base_commit', 'patch', 'sha256', 'files'}:
            raise ValueError('Invalid patch record fields')
        project = relative_name(record['project'])
        if project in projects:
            raise ValueError('Only one patch per project is supported: ' + project)
        projects.add(project)
        hex_value(record['base_commit'], 40)
        hex_value(record['sha256'], 64)
        patch = confined(series.parent / relative_name(record['patch']))
        record['data'] = patch.read_bytes()
        if digest(record['data']) != record['sha256']:
            raise ValueError('Patch hash mismatch: ' + str(patch))
        files = record['files']
        if not isinstance(files, list) or not files:
            raise ValueError('Patch must declare its touched files')
        names = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {
                    'path', 'before_sha256', 'after_sha256'}:
                raise ValueError('Invalid touched-file record')
            name = relative_name(item['path'])
            if name in names:
                raise ValueError('Duplicate touched file: ' + name)
            names.add(name)
            # None pins absence in both HEAD and the index for a new text file.
            if item['before_sha256'] is not None:
                hex_value(item['before_sha256'], 64)
            hex_value(item['after_sha256'], 64)
            if item['before_sha256'] == item['after_sha256']:
                raise ValueError('Touched file must change: ' + name)
        # New files must be ordinary non-executable text files. Other operations
        # still require an explicit source update instead of this patch helper.
        if (re.search(rb'^(?:old mode |new mode |deleted file mode |'
                     rb'rename from |rename to |copy from |copy to |GIT binary patch|'
                     rb'Binary files )', record['data'], re.M) or
                any(mode != b'100644' for mode in re.findall(
                    rb'^new file mode ([^\n]+)$', record['data'], re.M))):
            raise ValueError('Unsupported patch operation: ' + record['patch'])
    return records


def check_patch_paths(project, record):
    # Run at the project root: Git otherwise filters patch paths by cwd prefix.
    rows = git(project, 'apply', '--numstat', '-z', '-', data=record['data'])
    changed = []
    for row in rows.rstrip(b'\0').split(b'\0'):
        fields = row.split(b'\t', 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError('Unsupported patch file entry: ' + record['patch'])
        changed.append(relative_name(fields[2].decode()))
    names = {item['path'] for item in record['files']}
    if len(changed) != len(names) or set(changed) != names:
        raise ValueError('Patch paths do not match declared files: ' + record['patch'])


def state(tree, record):
    project = confined(tree / record['project'])
    top = git(project, 'rev-parse', '--show-toplevel').decode().strip()
    if Path(top) != project:
        raise ValueError('Project must be a Git checkout root: ' + str(project))
    check_patch_paths(project, record)
    head = git(project, 'rev-parse', 'HEAD').decode().strip()
    if head != record['base_commit']:
        raise ValueError('Base HEAD mismatch: ' + record['project'])
    names = [item['path'] for item in record['files']]
    if git(project, 'diff', '--cached', '--name-only', '-z', 'HEAD', '--', *names):
        raise ValueError('Preserving staged touched-file changes: ' + record['project'])
    states = set()
    originals = {}
    for item in record['files']:
        name = item['path']
        path = confined(project / name)
        if item['before_sha256'] is None:
            if git(project, 'ls-tree', '-z', 'HEAD', '--', name):
                raise ValueError('New file already exists in base: ' + name)
            if git(project, 'ls-files', '--stage', '-z', '--', name):
                raise ValueError('Preserving indexed new file: ' + name)
            # Creating a child must not replace a removed tracked file or gitlink
            # with a directory. Check both HEAD and index, including staged parents.
            for parent in PurePosixPath(name).parents:
                if str(parent) == '.':
                    break
                parent_path = confined(project / parent)
                if parent_path.exists() and not parent_path.is_dir():
                    raise ValueError('New-file parent is not a directory: ' + str(parent))
                entry = git(project, 'ls-tree', '-z', 'HEAD', '--', str(parent))
                if entry and not entry.startswith(b'040000 tree '):
                    raise ValueError('New-file parent is not a base directory: ' + str(parent))
                indexed = git(project, 'ls-files', '--stage', '-z', '--', str(parent))
                if any(row.endswith(b'\t' + str(parent).encode())
                       for row in indexed.rstrip(b'\0').split(b'\0')):
                    raise ValueError('Preserving indexed new-file parent: ' + str(parent))
            originals[name] = None
            if not path.exists():
                states.add('ready')
                continue
            if (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o111 or
                    digest(path.read_bytes()) != item['after_sha256']):
                raise ValueError('Preserving existing new-file path: ' + name)
            states.add('already-applied')
            continue
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('Touched file is not a regular file: ' + str(path))
        tracked = git(project, 'ls-files', '--stage', '-z', '--', name).split(b'\0')
        expected_mode = b'100755' if path.stat().st_mode & 0o111 else b'100644'
        if len(tracked) != 2 or not tracked[0].startswith(expected_mode + b' '):
            raise ValueError('Touched file is untracked or its mode changed: ' + name)
        original = git(project, 'show', 'HEAD:' + name)
        if digest(original) != item['before_sha256']:
            raise ValueError('Base file hash mismatch: ' + record['project'] + '/' + name)
        originals[name] = original
        current = digest(path.read_bytes())
        if current == item['before_sha256']:
            states.add('ready')
        elif current == item['after_sha256']:
            states.add('already-applied')
        else:
            raise ValueError('Preserving edited touched file: ' + record['project'] + '/' + name)
    if len(states) != 1:
        raise ValueError('Partially applied patch: ' + record['project'])
    result = states.pop()
    options = ['--reverse'] if result == 'already-applied' else []
    git(project, 'apply', '--check', '--whitespace=nowarn', *options, '-', data=record['data'])
    return result, originals


def check_result(record, originals):
    """Verify the declared after hashes without changing a source checkout."""
    temporary = confined(ROOT / '.tmp')
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='android-patch-check-', dir=temporary) as name:
        scratch = Path(name)
        git(scratch, 'init', '-q')
        for path, content in originals.items():
            if content is None:
                continue
            dest = scratch / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        git(scratch, 'apply', '--whitespace=nowarn', '-', data=record['data'])
        for item in record['files']:
            if digest((scratch / item['path']).read_bytes()) != item['after_sha256']:
                raise ValueError('Patch result hash mismatch: ' + record['project'] + '/' + item['path'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, default=ROOT / 'src/android')
    parser.add_argument('--series', type=Path, default=DEFAULT_SERIES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='check only (the default)')
    mode.add_argument('--apply', action='store_true', help='apply after all preflight checks pass')
    args = parser.parse_args()
    try:
        tree, series = confined(args.tree), confined(args.series)
        records = load_series(series)
        plans = []
        # Finish every project and patch check before the first source mutation.
        for record in records:
            current, originals = state(tree, record)
            check_result(record, originals)
            plans.append((record, current))
        for record, current in plans:
            print(current.upper() + ': ' + record['project'])
        if args.apply:
            # Recheck all inputs after preflight; concurrent source editing is unsupported.
            for record, expected in plans:
                if state(tree, record)[0] != expected:
                    raise ValueError('Source state changed during preflight: ' + record['project'])
            for record, current in plans:
                if current == 'already-applied':
                    continue
                if state(tree, record)[0] != 'ready':
                    raise ValueError('Source state changed before apply: ' + record['project'])
                project = tree / record['project']
                git(project, 'apply', '--whitespace=nowarn', '-', data=record['data'])
                if state(tree, record)[0] != 'already-applied':
                    raise ValueError('Source verification failed after apply: ' + record['project'])
                print('APPLIED: ' + record['project'])
        print(f'Checked {len(plans)} pinned Android patches')
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, 'error: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
