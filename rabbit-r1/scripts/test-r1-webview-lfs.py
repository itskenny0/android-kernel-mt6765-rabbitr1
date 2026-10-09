#!/usr/bin/env python3
"""Fault tests for targeted hydration using private files and a recording Git boundary."""
import copy, hashlib, importlib.util, io, json, tempfile, zipfile
from pathlib import Path
from unittest import mock

SCRIPT_DIR=Path(__file__).resolve().parent
OUT=Path('/rabbitr1/out/r1-webview-tests')
OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('ensure_webview',SCRIPT_DIR/'ensure-r1-webview.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
count=0

def rejected(fn):
    global count
    try:fn()
    except (ValueError,OSError,zipfile.BadZipFile):count+=1
    else:raise AssertionError('Unexpected success')

buf=io.BytesIO()
with zipfile.ZipFile(buf,'w') as z:
    z.writestr('AndroidManifest.xml',b'synthetic manifest, no runtime claim')
    z.writestr('lib/arm64-v8a/libwebview.so',b'synthetic payload')
payload=buf.getvalue()
pin=json.loads((SCRIPT_DIR.parent/'android/webview-lfs.json').read_text())
pin.update(bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
pointer=('version https://git-lfs.github.com/spec/v1\noid sha256:'+pin['sha256']+'\nsize '+str(len(payload))+'\n').encode()
with tempfile.TemporaryDirectory(dir=OUT,prefix='fixture-') as tmp:
    base=Path(tmp);m.ANDROID=base/'android';project=m.ANDROID/m.PROJECT;project.mkdir(parents=True)
    target=project/'webview.apk';cfg=base/'git-config';cfg.write_text('original repo skip-filter config\n')
    attributes=b'*.apk filter=lfs diff=lfs merge=lfs -text\n'
    lfsconfig=('[lfs]\n url = '+m.ENDPOINT+'\n').encode()
    calls=[];fault=None
    def reset():
        global fault
        fault=None;calls.clear();target.write_bytes(pointer)
        (project/'.gitattributes').write_bytes(attributes);(project/'.lfsconfig').write_bytes(lfsconfig)
        cfg.write_text('original repo skip-filter config\n')
    def fake(argv):
        calls.append(argv)
        if 'rev-parse' in argv:
            if argv[-1]=='HEAD':return ('0'*40 if fault=='wrong-head' else pin['commit'])+'\n'
            assert argv[-1]=='config';return str(cfg)+'\n'
        if 'show' in argv:
            name=argv[-1].split(':')[1]
            if name=='webview.apk':return ('bad pointer' if fault=='wrong-pointer' else pointer.decode())
            return {'.gitattributes':attributes,'.lfsconfig':lfsconfig}[name].decode()
        if 'config' in argv:return m.ENDPOINT+'\n'
        if 'diff' in argv:
            if fault=='staged':raise ValueError('staged changes')
            return ''
        if 'fetch' in argv:
            assert argv[-5:]==['fetch','--include=webview.apk','--exclude=','github',pin['commit']]
            assert '-c' in argv and 'lfs.url='+m.ENDPOINT in argv
            if fault=='fetch-fail':raise ValueError('fetch failed')
            if fault=='modified-during-fetch':target.write_bytes(b'local user change')
            return ''
        if 'checkout' in argv:
            assert argv[-3:]==['lfs','checkout','webview.apk']
            assert all(x in argv for x in m.OVERRIDES)
            if fault=='checkout-noop':return ''
            target.write_bytes(b'wrong' if fault=='bad-payload' else payload)
            if fault=='config-change':cfg.write_text('unexpected')
            return ''
        raise AssertionError(argv)
    with mock.patch.object(m,'run',side_effect=fake):
        reset();assert m.ensure(pin)['state']=='hydrated';count+=1
        assert any('fetch' in c for c in calls) and any('checkout' in c for c in calls)
        calls.clear();assert m.ensure(pin)['state']=='already-verified';count+=1
        assert not any('fetch' in c or 'checkout' in c for c in calls)
        calls.clear();assert m.ensure(pin,True)['state']=='already-verified';count+=1
        assert not any('fetch' in c or 'checkout' in c for c in calls)
        reset();rejected(lambda:m.ensure(pin,True));assert not any('fetch' in c for c in calls)
        for field,value in [('project','external/chromium-webview/prebuilt/arm'),('path','../webview.apk'),('remote','origin'),('lfs_url','https://example.invalid/lfs'),('commit','--bad'),('sha256','0'),('bytes',0)]:
            reset();p=copy.deepcopy(pin);p[field]=value;rejected(lambda:m.ensure(p));assert not calls
        for mode in ['wrong-head','wrong-pointer','staged','fetch-fail','modified-during-fetch','checkout-noop','bad-payload','config-change']:
            reset();fault=mode;rejected(lambda:m.ensure(pin))
            if mode=='modified-during-fetch':
                assert target.read_bytes()==b'local user change' and not any('checkout' in c for c in calls)
        for content in [b'locally modified pointer',b'Z'*len(payload)]:
            reset();target.write_bytes(content);rejected(lambda:m.ensure(pin));assert target.read_bytes()==content
            assert not any('fetch' in c or 'checkout' in c for c in calls)
        reset();(project/'.lfsconfig').write_bytes(b'modified config');rejected(lambda:m.ensure(pin));assert not any('fetch' in c for c in calls)
        reset();target.unlink();rejected(lambda:m.ensure(pin));assert not target.exists()
        reset();target.unlink();target.symlink_to(cfg);rejected(lambda:m.ensure(pin));assert target.is_symlink();target.unlink()
        reset();target.write_bytes(b'not-a-zip');p=copy.deepcopy(pin);p.update(bytes=9,sha256=hashlib.sha256(b'not-a-zip').hexdigest())
        rejected(lambda:m.verify_payload(target,p))
print('PASS:',count,'private targeted LFS hydration/refusal/fault cases; no network or Android mutation')
(OUT/'helper-test-result.json').write_text(json.dumps({'status':'pass','cases':count,'production_helper_sha256':hashlib.sha256((SCRIPT_DIR/'ensure-r1-webview.py').read_bytes()).hexdigest(),'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'network_access':False,'android_mutated':False},indent=2)+'\n')
