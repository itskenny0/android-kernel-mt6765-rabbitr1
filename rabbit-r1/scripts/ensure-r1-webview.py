#!/usr/bin/env python3
"""Hydrate only the reviewed r1 ARM64 WebView LFS asset; no repo sync/config edits."""
import argparse, hashlib, json, os, re, stat, subprocess, zipfile
from pathlib import Path

ROOT = Path('/rabbitr1')
ANDROID = ROOT/'src/android'
PROJECT = 'external/chromium-webview/prebuilt/arm64'
REPOSITORY = 'LineageOS/android_external_chromium-webview_prebuilt_arm64'
ENDPOINT = 'https://review.lineageos.org/'+REPOSITORY+'.git/info/lfs'
OVERRIDES = ['-c','filter.lfs.process=git-lfs filter-process',
             '-c','filter.lfs.smudge=git-lfs smudge -- %f',
             '-c','filter.lfs.clean=git-lfs clean -- %f',
             '-c','filter.lfs.required=true']


def require(ok, message):
    if not ok: raise ValueError(message)


def run(argv):
    p = subprocess.run(argv, capture_output=True, text=True,
                       env={**os.environ, 'GIT_OPTIONAL_LOCKS':'0', 'GIT_TERMINAL_PROMPT':'0'})
    # Avoid echoing authentication errors/signed URLs. All argv here are public,
    # fixed-path Git commands; no credentials or server responses are printed.
    require(p.returncode == 0, 'Git operation failed (exit %d): %s' %
            (p.returncode, argv[-1]))
    return p.stdout


def identity(p):
    s = p.lstat()
    return (s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


def verify_payload(path, pin):
    before = identity(path)
    require(stat.S_ISREG(before[2]) and before[3] == pin['bytes'], 'WebView size/type mismatch')
    with path.open('rb') as f:
        require(hashlib.file_digest(f,'sha256').hexdigest() == pin['sha256'], 'WebView SHA256 mismatch')
    with zipfile.ZipFile(path) as z:
        require('AndroidManifest.xml' in z.namelist() and z.testzip() is None, 'Invalid WebView ZIP/manifest')
    require(identity(path) == before, 'WebView changed during verification')


def ensure(pin, check_only=False):
    require(pin['project'] == PROJECT and pin['path'] == 'webview.apk'
            and pin['repository'] == REPOSITORY and pin['remote'] == 'github'
            and pin['lfs_url'] == ENDPOINT, 'Pin must name only the reviewed r1 ARM64 project/path/endpoint')
    require(re.fullmatch('[0-9a-f]{40}',pin['commit'])
            and re.fullmatch('[0-9a-f]{64}',pin['sha256'])
            and type(pin['bytes']) is int and 0 < pin['bytes'] < 2**32, 'Malformed WebView pin')
    project = ANDROID/PROJECT
    require(project.resolve() == project and project.is_relative_to(ROOT), 'Redirected WebView project')
    target = project/'webview.apk'
    require(not target.is_symlink() and target.is_file(), 'Missing/nonregular WebView; preserving local deletion/link')
    git = ['git','--no-optional-locks','-C',str(project)]
    require(run(git+['rev-parse','HEAD']).strip() == pin['commit'], 'WebView checkout differs from reviewed commit')
    expected = ('version https://git-lfs.github.com/spec/v1\noid sha256:'+pin['sha256']+
                '\nsize '+str(pin['bytes'])+'\n').encode()
    pointer = run(git+['show',pin['commit']+':webview.apk']).encode()
    require(pointer == expected, 'Committed WebView pointer differs from reviewed pin')
    # Both filter/endpoint files must still match their committed contents.
    for name in ['.gitattributes','.lfsconfig']:
        content = run(git+['show',pin['commit']+':'+name]).encode()
        require((project/name).is_file() and not (project/name).is_symlink()
                and (project/name).read_bytes() == content, 'Modified WebView '+name)
    require('*.apk filter=lfs diff=lfs merge=lfs -text' in (project/'.gitattributes').read_text(), 'Missing APK LFS attribute')
    endpoint = run(git+['config','--file',str(project/'.lfsconfig'),'--get','lfs.url']).strip()
    require(endpoint == ENDPOINT, 'Unexpected committed LFS endpoint')
    run(git+['diff','--cached','--exit-code','HEAD','--'])
    state = 'already-verified'
    if target.stat().st_size == len(expected) and target.read_bytes() == expected:
        require(not check_only, 'Unresolved ARM64 WebView LFS pointer')
        config_path = Path(run(git+['rev-parse','--path-format=absolute','--git-path','config']).strip())
        require(config_path.resolve().is_relative_to(ROOT), 'Git config outside workspace')
        config_before = config_path.read_bytes()
        run(git+['-c','lfs.url='+ENDPOINT,'lfs','fetch','--include=webview.apk','--exclude=',
                 'github',pin['commit']])
        require(not target.is_symlink() and target.is_file() and target.stat().st_size == len(expected)
                and target.read_bytes() == expected, 'Local WebView changed during fetch; preserving it')
        # Repo's skip filters and missing clean filter can make ordinary LFS
        # checkout return0 without hydrating. Per-command overrides install no
        # hooks/config and final payload verification remains mandatory.
        run(git+OVERRIDES+['lfs','checkout','webview.apk'])
        require(config_path.read_bytes() == config_before, 'Git config changed during hydration')
        state = 'hydrated'
    # Modified local content is not fetched over, even if its size looks right.
    verify_payload(target,pin)
    require(run(git+['rev-parse','HEAD']).strip() == pin['commit'], 'WebView HEAD changed during hydration')
    run(git+['diff','--cached','--exit-code','HEAD','--'])
    return {'state':state,'project':PROJECT,'commit':pin['commit'],
            'bytes':pin['bytes'],'sha256':pin['sha256'],'zip_crc_valid':True,
            'only_selected_arm64_asset':True,'git_config_or_hooks_installed':False}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--pin',required=True,type=Path)
    ap.add_argument('--check',action='store_true',help='Read-only verification; do not hydrate an unresolved pointer')
    a = ap.parse_args()
    try:
        require(a.pin.resolve().is_relative_to(ROOT), 'Pin outside workspace')
        result = ensure(json.loads(a.pin.read_text()), a.check)
        print(json.dumps(result,sort_keys=True))
    except (ValueError,OSError,KeyError,TypeError,zipfile.BadZipFile) as e:
        ap.exit(1,'WebView preflight failed: '+str(e)+'\n')

if __name__ == '__main__': main()
