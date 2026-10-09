#!/usr/bin/env python3
"""Fetch locked sources and archives, keeping all writable state in /rabbitr1."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request

ROOT = Path('/rabbitr1')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--profile', choices=['full', 'ci'], default='full',
                    help='ci fetches only mainline build and packaging dependencies')
args = parser.parse_args()
if not Path(__file__).resolve().is_relative_to(ROOT):
    raise SystemExit('This workspace must stay in /rabbitr1')
os.chdir(ROOT)
os.environ.update(TMPDIR=str(ROOT/'.tmp'), XDG_CACHE_HOME=str(ROOT/'.cache'),
                  GIT_CONFIG_GLOBAL=str(ROOT/'.gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                  PYTHONDONTWRITEBYTECODE='1', GIT_TERMINAL_PROMPT='0')
for name in ['.tmp','.cache','src','downloads','toolchains','logs']:
    (ROOT/name).mkdir(exist_ok=True)

def local(path):
    path = (ROOT/path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Path escapes /rabbitr1: '+str(path))
    return path

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

lock = json.loads((ROOT/'sources.lock.json').read_text())
if args.profile == 'ci':
    # A cached file must not hide a missing URL from clean runners.
    for name, item in lock['ci_files'].items():
        if not isinstance(item.get('url'), str) or not item['url'].startswith('https://'):
            raise SystemExit('CI input needs an HTTPS URL: '+name)

for name, repo in lock['repositories'].items():
    if args.profile == 'ci' and name not in ['mkbootimg', 'mtklkzap']:
        continue
    dest = local('src/'+name)
    if (dest/'.git').exists():
        head = run('git','-C',str(dest),'rev-parse','HEAD')
        if head != repo['commit'] and subprocess.run(
                ['git', '-C', str(dest), 'merge-base', '--is-ancestor', repo['commit'], head],
                capture_output=True).returncode:
            raise SystemExit(f'{dest}: HEAD differs from lock; preserving local work ({head})')
        if head != repo['commit']:
            print(name, 'preserving local descendant', head, flush=True)
    else:
        if dest.exists() and any(dest.iterdir()):
            raise SystemExit(f'{dest} is nonempty; preserving it')
        dest.mkdir(exist_ok=True)
        subprocess.run(['git','init',str(dest)],check=True)
        subprocess.run(['git','-C',str(dest),'remote','add','origin',repo['url']],check=True)
        subprocess.run(['git','-C',str(dest),'fetch','--depth=1','origin',repo['commit']],check=True)
        subprocess.run(['git','-C',str(dest),'checkout','--detach','FETCH_HEAD'],check=True)
    print(name, repo['commit'], flush=True)

for name, item in lock['archives'].items():
    if args.profile == 'ci' and name not in ['busybox-1.37.0.tar.bz2', 'rabbit_OS_v0.8.293.zip', 'mtklogo-v0.1.2.tgz']:
        continue
    dest = local('downloads/'+name)
    if not dest.exists():
        partial = dest.with_suffix(dest.suffix+'.part')
        with urllib.request.urlopen(item['url'], timeout=120) as response, partial.open('wb') as f:
            while chunk := response.read(1024*1024):
                f.write(chunk)
        if partial.stat().st_size != item['bytes'] or sha(partial) != item['sha256']:
            raise SystemExit('Download checksum mismatch: '+name)
        partial.rename(dest)
    if dest.stat().st_size != item['bytes'] or sha(dest) != item['sha256']:
        raise SystemExit('Archive checksum mismatch: '+name)
    if item['extract_to']:
        directory = local(item['extract_to'])
        marker = local('.cache/extracted-'+name+'.sha256')
        if not marker.exists() or marker.read_text().strip() != item['sha256']:
            directory.mkdir(parents=True,exist_ok=True)
            with tarfile.open(dest) as tar:
                strip = item.get('strip_components', 0)
                members = []
                for member in tar.getmembers():
                    parts = Path(member.name).parts
                    if len(parts) <= strip:
                        continue
                    member = member.replace(name=str(Path(*parts[strip:])))
                    if member.islnk() and strip:
                        member = member.replace(linkname=str(Path(*Path(member.linkname).parts[strip:])))
                    members.append(member)
                tar.extractall(directory, members=members, filter='data')
            marker.write_text(item['sha256']+'\n')
    print(name, 'SHA256 verified', flush=True)

if args.profile == 'ci':
    # Power-driver checks need pinned reference files, not the whole 4.19 tree.
    for name, item in lock['ci_files'].items():
        dest = local(name)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_suffix(dest.suffix+'.part')
            with urllib.request.urlopen(item['url'], timeout=120) as response, partial.open('wb') as f:
                while chunk := response.read(1024*1024):
                    f.write(chunk)
            if partial.stat().st_size != item['bytes'] or sha(partial) != item['sha256']:
                raise SystemExit('Download checksum mismatch: '+name)
            partial.rename(dest)
        if dest.stat().st_size != item['bytes'] or sha(dest) != item['sha256']:
            raise SystemExit('Reference checksum mismatch: '+name)
        print(name, 'SHA256 verified', flush=True)
    # Build exactly the checked-out commit. Applying patches here could hide a
    # regression in a pull request; check-patches.py verifies them separately.
    raise SystemExit(0)

src = local('src/mainline')
for patch in sorted((ROOT/'patches/mainline').glob('*.patch')):
    reverse = subprocess.run(['git','-C',str(src),'apply','--reverse','--check',str(patch)], capture_output=True)
    if reverse.returncode == 0:
        print(patch.name, 'already applied')
        continue
    subprocess.run(['git','-C',str(src),'apply','--check',str(patch)],check=True)
    subprocess.run(['git','-C',str(src),'apply','--intent-to-add',str(patch)],check=True)
    print(patch.name, 'applied')
