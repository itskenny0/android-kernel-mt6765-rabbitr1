#!/usr/bin/env python3
"""Record build identity and artifact hashes, without treating compilation as boot validation."""
import hashlib
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path('/rabbitr1')
target = sys.argv[1]
if target not in ('mainline', 'vendor'):
    raise SystemExit('Expected mainline or vendor')
src = ROOT / 'src' / ('mainline' if target == 'mainline' else 'kernel')
out = ROOT / 'out' / target
dist = ROOT / 'dist' / target

def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

# Only package this configuration's modules, not stale files in an O= tree.
def module_path(line):
    # 4.19 lists installation paths prefixed with kernel/; 7.1 lists .o files.
    if target == 'vendor':
        line = line.removeprefix('kernel/')
    return (out / line).with_suffix('.ko')

modules = sorted(module_path(line) for line in (out/'modules.order').read_text().splitlines())
for module in modules:
    if not module.resolve().is_relative_to(out) or not module.is_file():
        raise SystemExit('Invalid modules.order entry: '+str(module))
epoch = int(os.environ['SOURCE_DATE_EPOCH'])
with (dist / 'modules.tar.gz').open('wb') as f:
    with gzip.GzipFile(filename='', mode='wb', fileobj=f, mtime=epoch) as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            for module in modules:
                info = tar.gettarinfo(str(module), arcname=str(module.relative_to(out)))
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = epoch
                with module.open('rb') as source:
                    tar.addfile(info, source)

record = {
    'target': target,
    'status': 'compiled; not boot-tested; not a flashable firmware release',
    'kernel_release': (out / 'include/config/kernel.release').read_text().strip(),
    'source_commit': run('git', '-C', str(src), 'rev-parse', 'HEAD'),
    'source_diff_sha256': hashlib.sha256(subprocess.check_output(['git','-C',str(src),'diff','--binary','HEAD'])).hexdigest(),
    'untracked_source_files': {p: sha(src/p) for p in subprocess.check_output(
        ['git','-C',str(src),'ls-files','--others','--exclude-standard','-z'],text=True).split('\0') if p and (src/p).is_file()},
    'compiler': run('aarch64-linux-gnu-gcc' if target == 'mainline' else str(ROOT / 'toolchains/clang-r383902/bin/clang'), '--version'),
    'host_compiler': run('gcc','--version'),
    'source_date_epoch': os.environ.get('SOURCE_DATE_EPOCH'),
    'config_sha256': sha(out / '.config'),
    'module_count': len(modules),
    'modules': {str(p.relative_to(out)): sha(p) for p in modules},
    'artifacts': {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)} for p in sorted(dist.iterdir()) if p.is_file() and p.name not in ('build.json', 'SHA256SUMS')},
}
(dist / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
(dist / 'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in sorted(dist.iterdir()) if p.is_file() and p.name != 'SHA256SUMS'))
print(f'{target}: {record["kernel_release"]}; {len(modules)} modules; artifacts in {dist}')
