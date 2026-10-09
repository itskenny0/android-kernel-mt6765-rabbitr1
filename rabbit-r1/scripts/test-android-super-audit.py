#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Modeled SOC/legacy audit admission fixtures; never an executed image audit."""
import argparse, ast, contextlib, copy, hashlib, importlib.util, io, json, shutil, sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
BUILDER = HERE/'build-android-super.py'
FIXTURES = HERE.parent/'tests/android-super-audit'
spec = importlib.util.spec_from_file_location('audit_candidate', BUILDER)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
TOOL_SHA256 = 'e36cd66b46a04fedb9f23cc891b5b296f6779de7afcf6f08e3c67ee4b0bc7313'
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--lpmake', type=Path, default=m.ROOT/'toolchains/android-lp/bin/lpmake')
ap.add_argument('--lpmake-sha256', default=TOOL_SHA256)
ap.add_argument('--out', type=Path, default=m.WORK/'audit-profile-tests')
args = ap.parse_args()
TOOL = m.local(args.lpmake)
assert m.digest_file(TOOL)['sha256'] == args.lpmake_sha256, 'Pinned lpmake changed'

def output_path(value):
    path = m.local(value, output=True)
    if path.exists(): raise ValueError('Refusing existing test directory')
    return path

WORK = output_path(args.out); WORK.mkdir(parents=True)
provenance = json.loads((FIXTURES/'provenance.json').read_text())
producer = WORK/'producer'; producer.mkdir()
for name, row in provenance['producer_files'].items():
    source = FIXTURES/row['fixture']
    assert source.stat().st_size == row['bytes']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == row['sha256']
    assert source.suffix == '.txt' and not source.stat().st_mode & 0o111
    shutil.copyfile(source, producer/name)
# Process-wide audit hooks enforce data-only producer fixtures even if a future
# consumer accidentally imports one; no child process/tool runs in this suite.
# The 84-case sibling suite exercises the actual pinned lpmake independently.
guard = {'schema': False}
image_paths = set()
def data_only(event, arguments):
    if event == 'exec':
        path = Path(arguments[0].co_filename).absolute()
        if path.is_relative_to(producer) or path.is_relative_to(WORK/'changed-producer') or path.is_relative_to(FIXTURES/'producer'):
            raise AssertionError('Producer source fixtures must never execute')
    if event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'):
        raise AssertionError('Schema suite must not execute tools or child processes')
    if event == 'open' and guard['schema'] and isinstance(arguments[0], (str, bytes)):
        value = arguments[0].decode() if isinstance(arguments[0], bytes) else arguments[0]
        if str(Path(value).absolute()) in image_paths:
            raise AssertionError('Schema gate opened a modeled image')
sys.addaudithook(data_only)
# The three exact source snapshots are now runtime data at the required names.
tree=ast.parse((producer/'verify.py').read_text())
actual=[node.value.args[0].value for node in tree.body if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Name) and node.value.func.id=='check']
assert tuple(actual)==m.SOC_CHECKS and len(set(actual))==18
assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in producer.iterdir()}==m.SOC_PRODUCER

def file(name,data):
    p=WORK/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return m.digest_file(p)
def js(name,value):return file(name,(json.dumps(value,indent=2)+'\n').encode())
def model(label):return {'synthetic_schema_fixture':label}
source='1'*40;kernel=m.SOC_KERNEL
payloads={}
for i,name in enumerate((*m.PARTS,'super','vbmeta','boot','dtbo'),1):
    path=WORK/'payloads'/(name+'.img');path.parent.mkdir(exist_ok=True)
    if name in ('boot','dtbo'):
        with path.open('wb') as f:f.write(b'SYNTHETIC SCHEMA FIXTURE ONLY');f.truncate(33554432 if name=='boot' else 8388608)
    else:path.write_bytes(bytes([i])*4096)
    payloads[name]=m.digest_file(path)
image_paths={row['path'] for row in payloads.values()}
health_mode={'product':'lineage_r1_soc','experimental_soc_health':True,'soc_config':'y','embedded_config_verified':True,'config_sha256':'2'*64,'image_sha256':'3'*64,'image_gz_sha256':'4'*64}
installer=js('sources/.r1-install.json',{'synthetic_schema_fixture_only':True,'kernel_source_commit':kernel,'health_mode':health_mode})
kernel_record=js('sources/kernel-build.json',{'synthetic_schema_fixture_only':True,'source_commit':kernel})
source_inputs=[installer,kernel_record]+[file('sources/input-'+str(i),b'SYNTHETIC PINNED SOURCE '+str(i).encode()) for i in range(10)]
log=file('synthetic-build.log',b'SYNTHETIC SCHEMA FIXTURE; NO BUILD WAS PERFORMED\nAndroid configuration: lineage_r1_soc cp2a userdebug\n#### build completed successfully\n')
start=js('synthetic-build-start.json',{'synthetic_schema_fixture_only':True,'build_source_commit':source,'product':'lineage_r1_soc','release':'cp2a','variant':'userdebug','targets':sorted(m.SOC_TARGETS),'inputs':{'installer_sha256':installer['sha256']},'log':log['path']})
completed=js('synthetic-build-result.json',{'synthetic_schema_fixture_only':True,'exit_code':0,'inputs_unchanged':True,'build_source_commit':source,'build_start':start['path'],'build_start_sha256':start['sha256'],'log':log['path'],'log_sha256':log['sha256']})
initial={'build_start_commit':source,'installer_manifest':installer,'staged_files_verified':2,'canonical_templates_verified':1,'templates':{'fixture':{'sha256':'5'*64}},'generated_inputs_pinned_by_build_start':['fixture'],'kernel_source_commit':kernel,'android_patch_series_sha256':'6'*64,'sources_lock_sha256':'7'*64,'verified_applied_patches':[model('not a real Android patch') ]}
identity={'expected_source_commit':source,'expected_kernel_commit':kernel,'product':'lineage_r1_soc','release':'cp2a','completed_session':'123','build_start':start,'build_result':completed,'build_log':log,'source_identity':initial,'guard':m.digest_file(producer/'provenance.py')}
checks={}
for name,keys in m.SOC_DETAIL_KEYS.items():
    checks[name]={key:(m.SOC_BOOLEAN_DETAILS[key] if key in m.SOC_BOOLEAN_DETAILS else 'synthetic' if key in m.SOC_STRING_DETAILS else 1 if key in m.SOC_INTEGER_DETAILS else [model(key)] if key in m.SOC_LIST_DETAILS else model(key)) for key in keys}
checks[m.SOC_CHECKS[0]]={**initial,'build_log':log,'build_identity':identity,'audit_commit':'8'*40}
sizes={n:{**r,'android_sparse':n=='super','logical_bytes':m.LAYOUT['super_bytes'] if n=='super' else r['bytes'],'host_allocated_bytes':Path(r['path']).stat().st_blocks*512} for n,r in payloads.items()};total=sum(sizes[n]['bytes'] for n in m.PARTS)
sizes['dynamic_group']={'image_total_bytes':total,'maximum_bytes':m.LAYOUT['group_bytes'],'remaining_bytes':m.LAYOUT['group_bytes']-total};checks[m.SOC_CHECKS[1]]=sizes
checks[m.SOC_CHECKS[4]].update(populated_slot='a',empty_slot='b',logical_extent_image_sha256={n+'_a':payloads[n]['sha256'] for n in m.PARTS})
checks[m.SOC_CHECKS[8]].update(health_mode=health_mode,kernel_record=kernel_record,resolved_compiler_flag='clang-r596125',values={'DeviceProduct':'lineage_r1_soc','Platform_version_name':'17','Platform_sdk_version':37,'VendorApiLevel':'202604'})
linked={n:file('health/'+n,b'SYNTHETIC '+n.encode()) for n in ('vendor-service','initial-readiness','recovery-service')}
checks[m.SOC_CHECKS[9]]={'binaries':linked,'init_vintf':{'normal_boot_readiness_only':True}}
checks[m.SOC_CHECKS[10]]['compiled']={'normal':model('policy'),'recovery':model('policy')}
services=[{'partition':'vendor','path':'/etc/init/health.r1.rc','name':name,'command':argv} for name,argv in [('vendor.health-default',['/vendor/bin/hw/android.hardware.health-service.r1']),('r1-health-initial-ready',['/vendor/bin/r1-health-initial-ready']),('vendor.charger',['/vendor/bin/hw/android.hardware.health-service.r1','--charger'])]]
member_map={name:copy.deepcopy(linked[k]) for name,k in [('/bin/hw/android.hardware.health-service.r1','vendor-service'),('/bin/r1-health-initial-ready','initial-readiness')]}
checks[m.SOC_CHECKS[5]]['vendor']={'selected_files':member_map}
checks[m.SOC_CHECKS[11]].update(actual_vendor_image_members=member_map,normal_health_inventory={'cuttlefish_health_apex_absent':True,'normal_health_vintf_providers':[{'partition':'vendor','path':'/etc/vintf/manifest/health.r1.xml','format':'aidl','version':'5','fqnames':['IHealth/default']}],'normal_health_init_services':services})
checks[m.SOC_CHECKS[13]].update(boot=payloads['boot'],dtbo=payloads['dtbo'])
checks[m.SOC_CHECKS[15]]['minigbm_config']={'platform':'all_arm'}
checks[m.SOC_CHECKS[-1]]['source_records_and_full_artifact_hashes']={'unchanged':True,'source_commit':source,'watched_artifacts':30}
audit={'synthetic_schema_fixture_only':True,'scope':'MODELED RECORD SHAPES ONLY; NOT AN EXECUTED AUDIT OR COMPLETED ANDROID IMAGES','mode':'verify','overall':'pass','errors':[],'build_start_source_commit':source,'verifier':m.digest_file(producer/'verify.py'),'source_inputs':source_inputs,'expected_images':[r['path'] for r in payloads.values()],'checks':[{'name':n,'status':'pass','details':checks[n]} for n in m.SOC_CHECKS]}
AP=WORK/'synthetic-audit.json';count=0;failures=[]

def put(obj):AP.write_text(json.dumps(obj,indent=2)+'\n');return m.digest_file(AP)['sha256']
def inspect(obj,profile='soc-cp2a-18'):
    h=put(obj);real=m.digest_file
    def no_images(path):
        assert str(path) not in image_paths,'Schema gate opened a payload'
        return real(path)
    guard['schema']=True
    try:
        with mock.patch.object(m,'digest_file',side_effect=no_images):return m.read_audit(AP,h,profile)
    finally:guard['schema']=False
def reject(label,change,base=audit,profile='soc-cp2a-18'):
    global count
    obj=copy.deepcopy(base);change(obj)
    try:inspect(obj,profile)
    except (ValueError,KeyError,TypeError,StopIteration):count+=1;failures.append(label)
    else:raise AssertionError('Unexpected schema acceptance: '+label)
def details(obj,index):return obj['checks'][index]['details']

inspect(audit);count+=1
for i in range(18):
    reject('missing-check-'+str(i),lambda o,i=i:o['checks'].pop(i))
    reject('failed-check-'+str(i),lambda o,i=i:o['checks'][i].update(status='fail'))
    reject('empty-details-'+str(i),lambda o,i=i:o['checks'][i].update(details={}))
    reject('boolean-details-'+str(i),lambda o,i=i:o['checks'][i].update(details=True))
reject('extra-check',lambda o:o['checks'].append({'name':'invented','status':'pass','details':{'synthetic':True}}))
reject('duplicate-check',lambda o:o['checks'].__setitem__(1,copy.deepcopy(o['checks'][0])))
for field,value in [('mode','plan'),('overall','fail'),('errors',['incomplete'])]:reject(field,lambda o,f=field,v=value:o.__setitem__(f,v))
for field,value in [('expected_source_commit','9'*40),('expected_kernel_commit','9'*40),('product','lineage_r1'),('release','next'),('completed_session',True)]:
    reject('identity-'+field,lambda o,f=field,v=value:details(o,0)['build_identity'].__setitem__(f,v))
reject('empty-source-identity',lambda o:details(o,0)['build_identity'].__setitem__('source_identity',{}))
reject('source-input-pin',lambda o:o['source_inputs'][2].__setitem__('sha256','0'*64))
reject('missing-source-input',lambda o:o['source_inputs'].pop())
reject('missing-image',lambda o:details(o,1).pop('vendor'))
reject('image-path',lambda o:details(o,1)['vendor'].__setitem__('path',payloads['system']['path']))
reject('image-size-bool',lambda o:details(o,1)['boot'].__setitem__('bytes',True))
reject('group-mismatch',lambda o:details(o,1)['dynamic_group'].__setitem__('image_total_bytes',1))
reject('super-correspondence',lambda o:details(o,4)['logical_extent_image_sha256'].__setitem__('vendor_a','0'*64))
reject('unclosed-final-guard',lambda o:details(o,17)['source_records_and_full_artifact_hashes'].__setitem__('unchanged',False))
reject('final-source-mismatch',lambda o:details(o,17)['source_records_and_full_artifact_hashes'].__setitem__('source_commit','0'*40))
reject('wrong-soc-mode',lambda o:details(o,8)['health_mode'].__setitem__('experimental_soc_health',False))
reject('wrong-vendor-api',lambda o:details(o,8)['values'].__setitem__('VendorApiLevel','202704'))
reject('mock-health',lambda o:details(o,11)['normal_health_inventory'].__setitem__('cuttlefish_health_apex_absent',False))
reject('extra-provider',lambda o:details(o,11)['normal_health_inventory']['normal_health_vintf_providers'].append(model('mock provider')))
reject('extra-service',lambda o:details(o,11)['normal_health_inventory']['normal_health_init_services'].append({'name':'mock'}))
reject('health-staging-mismatch',lambda o:details(o,9)['binaries']['vendor-service'].__setitem__('sha256','0'*64))
reject('recovery-readiness',lambda o:details(o,9)['init_vintf'].__setitem__('normal_boot_readiness_only',False))
reject('policy-incomplete',lambda o:details(o,10).__setitem__('compiled',{'normal':model('policy')}))
reject('boot-mismatch',lambda o:details(o,13)['boot'].__setitem__('sha256','0'*64))
reject('allocator-x86',lambda o:details(o,15)['minigbm_config'].__setitem__('platform','intel'))
for i in (0,8,10,14):
    key=sorted(m.SOC_DETAIL_KEYS[m.SOC_CHECKS[i]])[0];reject('null-field-'+str(i),lambda o,i=i,k=key:details(o,i).__setitem__(k,None))
# Alter pinned build records, retaining coherent new file/hash pointers. They
# must fail their actual semantic linkage, not just an outer checksum.
for key,value in [('exit_code',1),('inputs_unchanged',False),('build_source_commit','9'*40),('build_start_sha256','0'*64),('log_sha256','0'*64)]:
    obj=json.loads(Path(completed['path']).read_text());obj[key]=value;bad=js('bad-result-'+key+'.json',obj)
    reject('completed-'+key,lambda o,bad=bad:details(o,0)['build_identity'].__setitem__('build_result',bad))
badproducer=WORK/'changed-producer';shutil.copytree(producer,badproducer);(badproducer/'health_checks.py').write_bytes((badproducer/'health_checks.py').read_bytes()+b'\n# changed producer\n')
reject('changed-imported-producer',lambda o:o.__setitem__('verifier',m.digest_file(badproducer/'verify.py')))
# Exact historical kernel/helper bindings must not enter the current profile.
# Keep the modeled old-kernel JSON chain internally consistent so these fail
# the intended admission boundary, not an incidental stale file/hash pointer.
historical_rejections=[]
def reject_historical(label, obj, expected_error):
    global count
    try:inspect(obj)
    except ValueError as error:
        assert str(error)==expected_error,(label,str(error))
        count+=1;failures.append(label)
        historical_rejections.append({'case':label,'error':str(error)})
    else:raise AssertionError('Historical binding accepted: '+label)

old_kernel='f72592a467a1e289ffbfd0fbee6adac5093bf6db'
old=copy.deepcopy(audit)
old_installer_data=json.loads(Path(installer['path']).read_text())
old_installer_data['kernel_source_commit']=old_kernel
old_installer=js('historical-kernel/installer.json',old_installer_data)
old_record_data=json.loads(Path(kernel_record['path']).read_text())
old_record_data['source_commit']=old_kernel
old_record=js('historical-kernel/kernel-build.json',old_record_data)
old_start_data=json.loads(Path(start['path']).read_text())
old_start_data['inputs']['installer_sha256']=old_installer['sha256']
old_start=js('historical-kernel/start.json',old_start_data)
old_result_data=json.loads(Path(completed['path']).read_text())
old_result_data.update(build_start=old_start['path'],build_start_sha256=old_start['sha256'])
old_result=js('historical-kernel/result.json',old_result_data)
old_provenance=details(old,0);old_identity=old_provenance['build_identity']
old_provenance.update(kernel_source_commit=old_kernel,installer_manifest=old_installer)
old_identity.update(expected_kernel_commit=old_kernel,build_start=old_start,build_result=old_result)
old_identity['source_identity'].update(kernel_source_commit=old_kernel,installer_manifest=old_installer)
old['source_inputs']=[old_installer,old_record,*old['source_inputs'][2:]]
details(old,8)['kernel_record']=old_record
assert old_identity['source_identity']=={k:old_provenance[k] for k in initial}
assert old_start_data['inputs']['installer_sha256']==old_provenance['installer_manifest']['sha256']
assert old_result_data['build_start']==old_identity['build_start']['path']
assert old_result_data['build_start_sha256']==old_identity['build_start']['sha256']
reject_historical('historical-f725-kernel',old,'Incomplete SOC build identity')

for name,constant,wanted in (
    ('provenance.py','EXPECTED_KERNEL=','82aad6709888a0779a380a71e263192e313bb425fcc239314a5c307443c05f3b'),
    ('health_checks.py','KERNEL_SOURCE = ','72a336069bc0ccabc70c058fabb5a0f8d19e9af6a96e311949050e09409aff8c')):
    directory=badproducer/('historical-'+name.removesuffix('.py'))
    shutil.copytree(producer,directory)
    path=directory/name;data=path.read_text();current=constant+repr(kernel)
    assert data.count(current)==1
    path.write_text(data.replace(current,constant+repr(old_kernel)))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==wanted
    old=copy.deepcopy(audit);old['verifier']=m.digest_file(directory/'verify.py')
    details(old,0)['build_identity']['guard']=m.digest_file(directory/'provenance.py')
    reject_historical('historical-'+name,old,'Unreviewed SOC audit producer: '+name)

# Explicit historical profile preserves reproduction while default SOC refuses
# to bless the known incomplete old Health-provider coverage.
legacy=copy.deepcopy(audit);legacy['checks']=[{'name':n,'status':'pass','details':{}} for n in m.LEGACY_CHECKS]
legacy['checks'][0]['details']={'build_start_commit':source,'build_log':log};legacy['checks'][1]['details']={n:sizes[n] for n in m.PARTS};legacy['checks'][-1]['details']={'image_sizes_and_mtimes_unchanged':True}
reject('legacy-rejected-by-default',lambda o:None,legacy)
inspect(legacy,'legacy-default-10');count+=1
reject('current-rejected-as-legacy',lambda o:None,audit,'legacy-default-10')
# Plan-only admission hashes the caller-supplied pinned tool, without executing
# it. All CLI paths use the actual main/argparse code under the data-only guard.
tool_sha=args.lpmake_sha256;h=put(audit)
p=m.make_plan(AP,h,TOOL,tool_sha);assert p['schema']==2 and p['audit_profile']=='soc-cp2a-18' and not p['historical_reproduction_only'];assert all(payloads[n] in p['audit_dependencies'] for n in ('super','vbmeta','boot','dtbo'));count+=1
h=put(legacy);p=m.make_plan(AP,h,TOOL,tool_sha,audit_profile='legacy-default-10');assert p['historical_reproduction_only'] and 'mock Health' in p['audit_limitations'][0];count+=1
def cli(obj, name, profile=None, expected=0):
    h=put(obj);dest=WORK/name
    argv=[str(BUILDER),'plan','--audit',str(AP),'--audit-sha256',h,
          '--lpmake',str(TOOL),'--lpmake-sha256',tool_sha,'--output',str(dest)]
    if profile is not None:argv+=['--audit-profile',profile]
    stderr=io.StringIO();code=0
    with mock.patch.object(sys,'argv',argv), contextlib.redirect_stderr(stderr):
        try:m.main()
        except SystemExit as exc:code=exc.code
    assert code==expected,(argv,code,stderr.getvalue())
    assert dest.exists()==(expected==0)
    return argv, json.loads(dest.read_text()) if dest.exists() else None

argv,cli_plan=cli(audit,'synthetic-current-plan.json')
assert cli_plan['audit_profile']=='soc-cp2a-18' and not cli_plan['historical_reproduction_only'];count+=1
_,cli_legacy=cli(legacy,'synthetic-explicit-legacy-plan.json','legacy-default-10')
assert cli_legacy['historical_reproduction_only'] and 'mock Health' in cli_legacy['audit_limitations'][0];count+=1
cli(legacy,'rejected-default-legacy.json',expected=1);count+=1
cli(audit,'rejected-current-as-legacy.json','legacy-default-10',expected=1);count+=1
cli(audit,'rejected-unknown-profile.json','invented',expected=2);count+=1
# Runtime guards themselves have controls. No actual producer code executes.
for producer_path in (producer/'verify.py', FIXTURES/'producer/verify.py.txt'):
    try:exec(compile('raise RuntimeError("must not run")',str(producer_path),'exec'),{})
    except AssertionError as exc:assert 'must never execute' in str(exc);count+=1
    else:raise AssertionError('Producer execution guard absent')
guard['schema']=True
try:
    try:Path(next(iter(image_paths))).read_bytes()
    except AssertionError as exc:assert 'opened a modeled image' in str(exc);count+=1
    else:raise AssertionError('Image open guard absent')
finally:guard['schema']=False
# Same output policy as the real builder; no rejected path is created.
for path in (WORK, m.ROOT/'outside-super-test', m.WORK.parent/'outside-super-test'):
    try:output_path(path)
    except ValueError:count+=1
    else:raise AssertionError('Unconfined or existing --out accepted')
link=WORK/'symlink-output';link.symlink_to(WORK/'absent-output',target_is_directory=True)
try:
    try:output_path(link)
    except ValueError:count+=1
    else:raise AssertionError('Symlink --out accepted')
finally:link.unlink()
assert output_path(WORK/'new-output')==WORK/'new-output';count+=1
# Verify all data fixtures and production consumer are still unchanged.
assert {name:hashlib.sha256((producer/name).read_bytes()).hexdigest() for name in m.SOC_PRODUCER}==m.SOC_PRODUCER

r={'status':'pass','cases':count,'rejections':failures,'historical_binding_rejections':historical_rejections,'scope':'Static source-schema and synthetic record fixtures ONLY; no current auditor or real Android image opened/executed','source_checks_match_ast':True,'producer_sha256':m.SOC_PRODUCER,'current_actual_audit_success_observed':False,'current_actual_images_ready':False,'cli_plan_argv':argv,'cli_exercised_in_process':True,'producer_execution_forbidden':True,'tool_execution_forbidden':True,'fixture_lookup':'relative to repository scripts directory','lpmake_sha256':tool_sha,'candidate_sha256':hashlib.sha256(BUILDER.read_bytes()).hexdigest(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'fixture_provenance_sha256':hashlib.sha256((FIXTURES/'provenance.json').read_bytes()).hexdigest()};(WORK/'test-result.json').write_text(json.dumps(r,indent=2)+'\n');print('PASS',count,'synthetic schema/profile cases; current real audit readiness remains unestablished')
