#!/usr/bin/env python3
"""Prepare or check the exact patched mtkclient tree offline (Linux, /rabbitr1)."""
import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

ROOT = Path('/rabbitr1')
PROJECT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_path(value):
    """Reject symlink components, including dangling links, without resolving them."""
    path = Path(os.path.abspath(value))
    require(path.is_relative_to(ROOT) and path != ROOT, 'path must be under /rabbitr1')
    for component in [*reversed(path.parents), path]:
        if component.is_relative_to(ROOT):
            try:
                require(not stat.S_ISLNK(component.lstat().st_mode),
                        f'symlink path component: {component}')
            except FileNotFoundError:
                pass
    return path


def regular_bytes(path):
    require(stat.S_ISREG(path.lstat().st_mode), f'not a regular file: {path}')
    with path.open('rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'input type changed')
        return stream.read()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    require(isinstance(value, str) and value and '\\' not in value,
            'invalid relative name')
    path = PurePosixPath(value)
    require(path.parts and not path.is_absolute() and str(path) == value and
            all(part not in ('.', '..') for part in path.parts), 'unsafe relative name')
    return value


def pin(data, spec, label, prefix=''):
    expected = spec[prefix + 'sha256']
    size = spec[prefix + 'bytes']
    require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected),
            f'invalid {label} hash')
    require(type(size) is int and size >= 0, f'invalid {label} size')
    require(len(data) == size and digest(data) == expected, f'{label} pin mismatch')


def read_inputs(manifest_path, archive_override=None, patch_override=None):
    manifest_bytes = regular_bytes(local_path(manifest_path))
    manifest = json.loads(manifest_bytes)
    require(set(manifest) == {'schema', 'archive', 'patch', 'files'} and
            type(manifest['schema']) is int and manifest['schema'] == 1,
            'unsupported manifest')
    archive_spec, patch_spec = manifest['archive'], manifest['patch']
    require(set(archive_spec) == {'filename', 'url', 'sha256', 'bytes', 'prefix'},
            'invalid archive fields')
    require(set(patch_spec) == {'path', 'sha256', 'bytes'}, 'invalid patch fields')
    filename = relative(archive_spec['filename'])
    require('/' not in filename, 'archive filename must be a basename')
    relative(patch_spec['path'])
    prefix = archive_spec['prefix']
    require(isinstance(prefix, str) and prefix.endswith('/') and
            '/' not in relative(prefix[:-1]), 'invalid archive prefix')
    archive = regular_bytes(local_path(archive_override or ROOT / 'downloads' / filename))
    patch = regular_bytes(local_path(patch_override or PROJECT / patch_spec['path']))
    pin(archive, archive_spec, 'archive')
    pin(patch, patch_spec, 'patch')
    require(isinstance(manifest['files'], list) and len(manifest['files']) == 3,
            'expected exactly three patched files')
    changes = {}
    for entry in manifest['files']:
        require(set(entry) == {'path', 'before_sha256', 'after_sha256',
                               'before_bytes', 'after_bytes'}, 'invalid changed-file fields')
        name = relative(entry['path'])
        require(name not in changes, 'duplicate changed file')
        changes[name] = entry
    files, directories = {}, {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for member in tar:
            name = member.name
            require(name == prefix[:-1] or name.startswith(prefix), 'archive prefix mismatch')
            name = '' if name == prefix[:-1] else relative(name[len(prefix):])
            require(name not in files and name not in directories, 'duplicate archive member')
            require(member.mode & ~0o777 == 0, 'special archive mode')
            if member.isdir():
                directories[name] = member.mode
            else:
                require(member.isfile() and name, 'unsupported archive member type')
                data = tar.extractfile(member).read()
                require(len(data) == member.size, 'short archive member')
                files[name] = {'mode': member.mode, 'data': data,
                               'sha256': digest(data), 'bytes': len(data)}
    require('' in directories and files, 'missing archive root/files')
    for name in [*files, *directories]:
        if name:
            parent = str(PurePosixPath(name).parent)
            require(('' if parent == '.' else parent) in directories,
                    'missing archive parent directory')
    for name, change in changes.items():
        require(name in files, 'patched file absent from archive')
        pin(files[name]['data'], change, name, 'before_')
        # Validate the final pin shape before starting any writes.
        require(isinstance(change['after_sha256'], str) and
                re.fullmatch('[0-9a-f]{64}', change['after_sha256']) and
                type(change['after_bytes']) is int and change['after_bytes'] > 0,
                'invalid after pin')
    return manifest_bytes, patch, files, directories, changes


def verify_tree(tree, files, directories, changes):
    require(stat.S_ISDIR(tree.lstat().st_mode), 'destination is not a directory')
    observed_files, observed_directories = set(), {''}
    require(stat.S_IMODE(tree.stat().st_mode) == directories[''], 'root mode mismatch')
    for parent, dirs, names in os.walk(tree, followlinks=False):
        for name in dirs + names:
            path = Path(parent) / name
            rel = path.relative_to(tree).as_posix()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                require(rel in directories and stat.S_IMODE(info.st_mode) == directories[rel],
                        f'unexpected directory/mode: {rel}')
                observed_directories.add(rel)
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and rel in files,
                        f'unexpected file/link/type: {rel}')
                expected = files[rel]
                require(stat.S_IMODE(info.st_mode) == expected['mode'], f'file mode mismatch: {rel}')
                data = regular_bytes(path)
                pin(data, changes.get(rel, expected), rel, 'after_' if rel in changes else '')
                observed_files.add(rel)
    require(observed_files == set(files) and observed_directories == set(directories),
            'tree has missing entries')


def publish_no_replace(source, destination):
    """Atomic no-clobber visibility on Linux; not a power-loss durability promise."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as error:
        raise RuntimeError('Linux renameat2 is required; no unsafe fallback') from error
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def prepare(destination, files, directories, changes, patch):
    require(not os.path.lexists(destination), 'destination already exists; use --check')
    require(destination.parent.is_dir(), 'destination parent must already exist')
    staging = Path(tempfile.mkdtemp(prefix='.mtkclient-prepare-', dir=destination.parent))
    try:
        os.chmod(staging, directories[''])
        for name in sorted(directories, key=lambda value: (value.count('/'), value)):
            if name:
                path = staging / name
                path.mkdir()
                os.chmod(path, directories[name])
        for name, item in files.items():
            path = staging / name
            with path.open('xb') as stream:
                stream.write(item['data'])
            os.chmod(path, item['mode'])
        env = dict((key, value) for key, value in os.environ.items() if not key.startswith('GIT_'))
        env.update(GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=str(ROOT / '.gitconfig'), GIT_TERMINAL_PROMPT='0')
        for check in (True, False):
            command = ['git', '-c', 'core.autocrlf=false', 'apply', '--no-index', '--whitespace=nowarn']
            if check:
                command.append('--check')
            subprocess.run(command + ['-'], cwd=staging, env=env, input=patch,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        # git apply recreates edited files using the process umask. This patch
        # changes bytes only; retain the archive's original permission bits.
        for name in changes:
            path = staging / name
            require(stat.S_ISREG(path.lstat().st_mode), 'patched file type changed')
            os.chmod(path, files[name]['mode'])
        verify_tree(staging, files, directories, changes)
        publish_no_replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=PROJECT / 'mtkclient/transport.json')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--patch', type=Path)
    parser.add_argument('--destination', type=Path, default=ROOT / 'src/mtkclient-haretic')
    parser.add_argument('--check', action='store_true', help='validate only; never repair')
    args = parser.parse_args()
    destination = local_path(args.destination)
    original = ROOT / 'src/mtkclient'
    require(not destination.is_relative_to(original) and not original.is_relative_to(destination),
            'original mtkclient tree is protected')
    manifest, patch, files, directories, changes = read_inputs(args.manifest, args.archive, args.patch)
    if args.check:
        verify_tree(destination, files, directories, changes)
    else:
        prepare(destination, files, directories, changes, patch)
    print(json.dumps({'status': 'checked' if args.check else 'prepared',
                      'destination': str(destination), 'files': len(files),
                      'directories': len(directories), 'manifest_sha256': digest(manifest),
                      'patch_sha256': digest(patch)}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, KeyError, TypeError,
            tarfile.TarError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'mtkclient preparation rejected: {error}')
