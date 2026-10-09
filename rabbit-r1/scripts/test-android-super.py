#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Synthetic structure/payload tests using the real pinned AOSP lpmake binary."""
import argparse, copy, errno, hashlib, importlib.util, json, os, shutil, struct, subprocess, sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dual', HERE/'build-android-super.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
TOOL_SHA256 = 'e36cd66b46a04fedb9f23cc891b5b296f6779de7afcf6f08e3c67ee4b0bc7313'
ap_cli = argparse.ArgumentParser()
ap_cli.add_argument('--lpmake', type=Path, default=Path(
    '/rabbitr1/src/android/prebuilts/kernel-build-tools/linux-x86/bin/lpmake'))
ap_cli.add_argument('--lpmake-sha256', default=TOOL_SHA256,
                    help='Reviewed lpmake digest; defaults to the tested AOSP prebuilt')
ap_cli.add_argument('--out', type=Path, default=m.WORK/'tests')
args = ap_cli.parse_args()
TOOL = m.local(args.lpmake)
assert m.digest_file(TOOL)['sha256'] == args.lpmake_sha256, 'Tests require the pinned lpmake binary'
WORK = m.local(args.out, output=True)
assert not WORK.exists(), 'Refusing existing test directory'
WORK.mkdir(parents=True)
D = dict(m.LAYOUT, super_bytes=16*1024*1024, group_bytes=4*1024*1024)
count = 0


def rejects(fn, message=''):
    global count
    try: fn()
    except (ValueError, OSError, KeyError, TypeError, struct.error) as e:
        if message: assert message in str(e), (message, str(e))
        count += 1
    else: raise AssertionError('Unexpected acceptance')


# This report is explicitly a mock audit of synthetic payload bytes. It is not
# evidence of completed Android images, AVB, ext4 or successful ROM compilation.
images = {}
for i, name in enumerate(m.PARTS, 1):
    p = WORK/(name+'.img'); p.write_bytes(bytes([i])*4096*i)
    images[name] = dict(m.digest_file(p), logical_bytes=p.stat().st_size, android_sparse=False)
log = WORK/'synthetic-success.log'; log.write_text('SYNTHETIC TEST ONLY\n#### build completed successfully\n')
audit = {'mode':'verify','overall':'pass','errors':[], 'synthetic_fixture_only':True,
         'build_start_source_commit':'1'*40,'verifier':m.digest_file(HERE/'test-android-super.py'),
         'checks':[{'name':n,'status':'pass','details':{}} for n in m.CHECKS]}
audit['checks'][0]['details'] = {'build_start_commit':'1'*40,'build_log':m.digest_file(log)}
audit['checks'][1]['details'] = images
audit['checks'][-1]['details'] = {'image_sizes_and_mtimes_unchanged':True}
ap = WORK/'synthetic-audit.json'

def write_audit(obj=audit):
    ap.write_text(json.dumps(obj,indent=2)+'\n')
    return m.digest_file(ap)['sha256']

def plan(obj=audit, dims=D):
    return m.make_plan(ap,write_audit(obj),TOOL,m.digest_file(TOOL)['sha256'],dims)

p = plan(); (WORK/'plan.json').write_text(json.dumps(p,indent=2)+'\n')
out = WORK/'good'
assert not out.exists(), 'Use fresh fixture output per test invocation'
r = m.build(p,out); count += 1
assert r['status']=='verified-both-slots-populated-raw-super'
assert r['validation']['group_used_bytes']=={'a':40960,'b':40960}
assert len(r['validation']['nonoverlapping_extents'])==8
assert len(set((a,b) for a,b,n in r['validation']['nonoverlapping_extents']))==8
argv = r['command']; assert '--sparse' not in argv and '--auto-slot-suffixing' not in argv and '--virtual-ab' not in argv
assert argv.count('--partition')==argv.count('--image')==8
assert {argv[i+1] for i,a in enumerate(argv) if a=='--partition'} == {
    f'{n}_{slot}:none:{images[n]["bytes"]}:r1_dynamic_partitions_{slot}'
    for n in m.PARTS for slot in 'ab'}
count += 1

# Reject all completion/provenance failures before opening any logical image.
for change in ['mode','overall','errors','missing','duplicate','failed','commit','immutability','audit-hash']:
    obj=copy.deepcopy(audit)
    if change=='mode':obj['mode']='plan'
    elif change=='overall':obj['overall']='fail'
    elif change=='errors':obj['errors']=['unfinished']
    elif change=='missing':obj['checks'].pop()
    elif change=='duplicate':obj['checks'][1]=obj['checks'][0]
    elif change=='failed':obj['checks'][2]['status']='fail'
    elif change=='commit':obj['build_start_source_commit']='2'*40
    elif change=='immutability':obj['checks'][-1]['details']['image_sizes_and_mtimes_unchanged']=False
    wanted=write_audit(obj)
    if change=='audit-hash':wanted='0'*64
    real=m.digest_file
    def guard(path):
        assert str(path) not in {row['path'] for row in images.values()}, 'Incomplete audit opened an image'
        return real(path)
    with mock.patch.object(m,'digest_file',guard):
        rejects(lambda:m.make_plan(ap,wanted,TOOL,real(TOOL)['sha256'],D))
write_audit()
for change in ['hash','size','sparse','unaligned','empty','budget','missing-image','bad-verifier','bad-log']:
    obj=copy.deepcopy(audit); row=obj['checks'][1]['details']['system']
    if change=='hash':row['sha256']='0'*64
    elif change=='size':row['bytes']+=4096;row['logical_bytes']=row['bytes']
    elif change=='sparse':row['android_sparse']=True
    elif change=='unaligned':row['bytes']=row['logical_bytes']=4095
    elif change=='empty':row['bytes']=row['logical_bytes']=0
    elif change=='budget':row['bytes']=row['logical_bytes']=D['group_bytes']+4096
    elif change=='missing-image':del obj['checks'][1]['details']['system']
    elif change=='bad-verifier':obj['verifier']['sha256']='0'*64
    elif change=='bad-log':obj['checks'][0]['details']['build_log']['sha256']='0'*64
    rejects(lambda:plan(obj))
write_audit()
rejects(lambda:plan(dims=dict(D,group_bytes=20000)), 'per-group')
rejects(lambda:plan(dims=dict(D,super_bytes=8*1024*1024)), 'headroom')
rejects(lambda:m.make_plan(ap,m.digest_file(ap)['sha256'],TOOL,'0'*64,D), 'executable')
rejects(lambda:m.build(p,out), 'existing')
q=copy.deepcopy(p);q['images']['system']['sha256']='0'*64
rejects(lambda:m.build(q,WORK/'stale-plan'), 'Stale')

raw=out/'super-both.raw.img'
base=raw.read_bytes(); geometry=base[4096:8192]
meta=base[12288:12288+D['metadata_bytes']]

def metadata_edit(fn):
    data=bytearray(base);blob=bytearray(meta);fn(blob)
    hs=struct.unpack_from('<I',blob,8)[0];ts=struct.unpack_from('<I',blob,44)[0]
    blob[48:80]=hashlib.sha256(blob[hs:hs+ts]).digest()
    blob[12:44]=bytes(32);blob[12:44]=hashlib.sha256(blob[:hs]).digest()
    for i in range(6):data[12288+i*65536:12288+(i+1)*65536]=blob
    return data


def bad(data, message=''):
    dest=WORK/'mutated.raw.img';dest.write_bytes(data)
    try:rejects(lambda:m.validate_raw(dest,p),message)
    finally:dest.unlink()

for offset in [4096,8192,12288,12288+65536,12288+2*65536,12288+3*65536,12288+4*65536,12288+5*65536]:
    b=bytearray(base);b[offset]^=1;bad(b)
# Valid checksum, invalid semantic structures must also be rejected.
hs=struct.unpack_from('<I',meta,8)[0]
po,pc,pstride=struct.unpack_from('<III',meta,80)
eo,ec,estride=struct.unpack_from('<III',meta,92)
go,gc,gstride=struct.unpack_from('<III',meta,104)
do,dc,dstride=struct.unpack_from('<III',meta,116)
def u32(at,value):return lambda b:struct.pack_into('<I',b,at,value)
def u64(at,value):return lambda b:struct.pack_into('<Q',b,at,value)
bad(metadata_edit(u32(hs+po+36,1)), 'attributes')
bad(metadata_edit(u32(hs+po+48,0)), 'group')
bad(metadata_edit(u32(hs+po+pstride+44,0)), 'populated')
bad(metadata_edit(u32(hs+eo+8,1)), 'extent')
bad(metadata_edit(u64(hs+eo+12,D['super_bytes']//512)), 'extent')
bad(metadata_edit(u64(hs+eo+12,1)), 'extent')
bad(metadata_edit(u64(hs+eo,1)), 'extent')
bad(metadata_edit(u64(hs+go+gstride+40,D['group_bytes']+4096)), 'group')
bad(metadata_edit(u32(hs+do+60,1)), 'geometry')
bad(metadata_edit(u32(hs+po+40,ec)), 'extents')
# Aliasing B onto A preserves its expected hash but must fail physical overlap.
firstsector=struct.unpack_from('<Q',meta,hs+eo+12)[0]
bad(metadata_edit(u64(hs+eo+estride+12,firstsector)), 'overlap')
for at,end,n in r['validation']['nonoverlapping_extents']:
    b=bytearray(base);b[at]^=1;bad(b,'Payload')
bad(base[:-4096], 'physical size')
bad(base+b'\0'*4096, 'physical size')
b=bytearray(base);b[:4]=bytes.fromhex('3aff26ed');bad(b,'Reserved')

# An actual A-only factory construction is not accepted as both-slot-ready.
a_only=WORK/'a-only.raw.img';args=m.command(p,a_only)
for i,arg in enumerate(args):
    if arg=='--partition' and args[i+1].split(':')[0].endswith('_b'):
        bits=args[i+1].split(':');bits[2]='0';args[i+1]=':'.join(bits)
args2=[];i=0
while i<len(args):
    if args[i]=='--image' and args[i+1].split('=')[0].endswith('_b'):i+=2
    else:args2.append(args[i]);i+=1
with (WORK/'a-only-lpmake.log').open('wb') as f:
    subprocess.run(args2,check=True,stdout=f,stderr=subprocess.STDOUT)
rejects(lambda:m.validate_raw(a_only,p),'populated');a_only.unlink()

# Genuine lpmake failure never leaves a raw image labeled ready.
write_audit();fresh=plan();failure=WORK/'tool-failure'
with mock.patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],7)):
    rejects(lambda:m.build(fresh,failure),'lpmake failed')
assert json.loads((failure/'result.json').read_text())['status']=='incomplete'
assert not (failure/'super-both.raw.img').exists()

# A self-contained plan-only report cannot bypass the incomplete-image gate.
# No actual or unfinished Android image/audit is accessed by this suite.
real_audit=WORK/'incomplete-audit.json'
plan_only=copy.deepcopy(audit);plan_only['mode']='plan'
real_audit.write_text(json.dumps(plan_only,indent=2)+'\n')
reject_output=WORK/'rejected-plan.json'
args=[sys.executable,str(HERE/'build-android-super.py'),'plan','--audit',str(real_audit),
      '--audit-sha256',m.digest_file(real_audit)['sha256'],'--lpmake',str(TOOL),
      '--lpmake-sha256',m.digest_file(TOOL)['sha256'],'--output',str(reject_output)]
proc=subprocess.run(args,capture_output=True,text=True)
assert proc.returncode and 'completed passing image audit' in proc.stderr and not reject_output.exists()
(WORK/'incomplete-gate.json').write_text(json.dumps({'argv':args,'exit':proc.returncode,'stderr':proc.stderr},indent=2)+'\n')
count+=1

# A mislabeled sparse header must fail even if the audit's byte hash matches.
source=Path(images['system']['path']); original=source.read_bytes()
source.write_bytes(bytes.fromhex('3aff26ed')+original[4:])
obj=copy.deepcopy(audit);obj['checks'][1]['details']['system'].update(m.digest_file(source))
try: rejects(lambda:plan(obj),'Sparse file mislabeled')
finally: source.write_bytes(original);write_audit()

# Actual tool output followed by transport corruption or source mutation is
# removed; neither successful exit0 nor a stale previous plan blesses it.
real_run=m.subprocess.run
for fault in ['output-corruption','input-mutation','late-input-touch']:
    fresh=plan(); destdir=WORK/fault
    def tool_mutation(args,**kwargs):
        got=real_run(args,**kwargs)
        if fault=='output-corruption':
            path=Path(args[-1])
            with path.open('r+b') as f:f.seek(4096);f.write(b'!')
        elif fault=='input-mutation':source.write_bytes(b'!'+original[1:])
        return got
    validator=m.validate_raw
    def late_touch(path,plan):
        got=validator(path,plan)
        if fault=='late-input-touch':source.write_bytes(original)
        return got
    try:
        with mock.patch.object(m.subprocess,'run',side_effect=tool_mutation), mock.patch.object(m,'validate_raw',side_effect=late_touch):
            rejects(lambda:m.build(fresh,destdir))
        assert json.loads((destdir/'result.json').read_text())['status']=='incomplete'
        assert not (destdir/'super-both.raw.img').exists()
    finally:source.write_bytes(original);write_audit()

# Reject special/symlink input paths before reading their contents.
pipe=WORK/'rejected.fifo';os.mkfifo(pipe)
try:rejects(lambda:m.digest_file(pipe),'regular')
finally:pipe.unlink()
link=WORK/'rejected-link.img';link.symlink_to(source)
try:rejects(lambda:m.digest_file(link),'symlink')
finally:link.unlink()

# Failure to publish the result must never leave a success record or skip raw
# cleanup, even if both the initial write and incomplete-report write fail.
real_replace=m.os.replace
for fault in ['publish-once', 'publish-persistent', 'report-write-persistent']:
    fresh=plan(); destdir=WORK/fault; statuses=[]
    original_publish=m.publish_result
    def tracked_publish(directory,result):
        statuses.append(result['status'])
        return original_publish(directory,result)
    def failing_replace(source,target):
        if Path(target)==destdir/'result.json':
            assert not Path(target).exists(), 'Prematurely published result'
            if fault=='publish-persistent' or (fault=='publish-once' and len(statuses)==1):
                raise OSError(errno.ENOSPC, 'modeled result publication failure')
        return real_replace(source,target)
    real_open=Path.open
    def failing_open(path,*args,**kwargs):
        if fault=='report-write-persistent' and path==destdir/'result.json.tmp':
            raise OSError(errno.ENOSPC, 'modeled result write failure')
        return real_open(path,*args,**kwargs)
    with mock.patch.object(m,'publish_result',side_effect=tracked_publish), \
         mock.patch.object(m.os,'replace',side_effect=failing_replace), \
         mock.patch.object(Path,'open',failing_open):
        rejects(lambda:m.build(fresh,destdir),'modeled result')
    assert statuses==['verified-both-slots-populated-raw-super','incomplete']
    assert not (destdir/'super-both.raw.img').exists()
    assert not (destdir/'result.json.tmp').exists()
    if fault=='publish-once':
        record=json.loads((destdir/'result.json').read_text())
        assert record['status']=='incomplete' and record['error']
    else:
        assert not (destdir/'result.json').exists()

# Pin all original independent tool outputs and explicit synthetic semantics.
result={'status':'pass','cases':count,'synthetic_payloads_only':True,
        'actual_lpmake':m.digest_file(TOOL),'layout':D,'good_result':r,
        'real_android_image_audit_available':False,'real_r1_super_built':False}
(WORK/'test-results.json').write_text(json.dumps(result,indent=2)+'\n')
print('PASS:',count,'synthetic plan/build/metadata/payload/failure cases using actual AOSP lpmake')
print('No real Android filesystem image was opened; no hardware/device operation was performed.')
