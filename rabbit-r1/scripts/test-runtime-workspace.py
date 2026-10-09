#!/usr/bin/env python3
"""Check explicit host paths with local fixtures; never import USB or target code."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shlex
from pathlib import Path
import shutil
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
PROJECT = SCRIPTS.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', required=True, type=Path)
args = parser.parse_args()
if not args.out.is_absolute() or not args.out.is_relative_to('/rabbitr1') or '..' in args.out.parts or args.out.exists():
    parser.error('--out must be a new absolute directory below /rabbitr1')
args.out.mkdir(parents=True)

def module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value

flash = module('runtime_flash', SCRIPTS/'prepare-flash.py')
checker = module('runtime_checker', SCRIPTS/'prepare-mtkclient.py')
controls = []
def note(name): controls.append(name)
def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), **kw)
def require_failure(argv, phrase=None, **kw):
    p = run(argv, **kw)
    assert p.returncode, (argv, p.stdout, p.stderr)
    if phrase: assert phrase in p.stderr+p.stdout, (argv, p.stdout, p.stderr)
    return p

remote = {'schema':1,'workspace':'/opt/rabbit','directory':'/opt/rabbit/prepared/diagnostic-a',
          'archive':'/opt/rabbit/downloads/mtkclient-v2.1.4.1.tar.gz',
          'destination':'/opt/rabbit/mtkclient-haretic-cid',
          'python':'/opt/rabbit/venv-mtkclient/bin/python','tmpdir':'/opt/rabbit/.tmp',
          'cache':'/opt/rabbit/.cache','config':'/opt/rabbit/.cache/config','data':'/opt/rabbit/.cache/data'}
assert flash.runtime_metadata(remote) == remote
note('remote metadata admitted lexically without resolving or inspecting remote paths')
for key,value in [('workspace','/'),('workspace','//opt/rabbit'),('workspace','/opt/rabbit/..'),
                  ('workspace','/opt/rabbit\n'),('python','relative/python'),('tmpdir','/outside/tmp'),
                  ('destination','/opt/rabbit/src/mtkclient'),('destination','/opt/rabbit'),
                  ('directory','/opt/rabbit/mtkclient-haretic-cid/prepared'),
                  ('archive','/opt/rabbit/other.tar.gz'),('schema',True),('unexpected','value')]:
    bad = dict(remote); bad[key] = value
    try: flash.runtime_metadata(bad)
    except ValueError: pass
    else: raise AssertionError((key,value))
    note('runtime rejected '+key+'='+repr(value))

expected_cid = '0123456789abcdef0123456789abcdef'  # synthetic, never a device identity
with_cid = dict(remote, expected_emmc_cid=expected_cid.upper())
assert flash.runtime_metadata(with_cid)['expected_emmc_cid'] == expected_cid
note('optional expected CID normalized to lowercase')
for bad_cid in (None, True, 123, '0'*31, 'g'*32, expected_cid+';', '0'*32, 'f'*32, 'F'*32):
    try: flash.runtime_metadata(dict(remote,expected_emmc_cid=bad_cid))
    except ValueError: pass
    else: raise AssertionError(bad_cid)
    note('malformed expected CID rejected '+repr(bad_cid))

# Local workspace paths intentionally contain shell metacharacters. All generated
# commands must pass them as data; the recorder refuses the first target command.
workspace = args.out / "workspace ' $(touch INJECTION)"
workspace.mkdir()
runtime = dict(remote)
for key in remote.keys() - {'schema','workspace'}:
    runtime[key] = str(workspace / Path(remote[key]).relative_to('/opt/rabbit'))
runtime['workspace'] = str(workspace)
runtime['expected_emmc_cid'] = expected_cid
out = Path(runtime['directory']); out.mkdir(parents=True)
for key in ('tmpdir','cache','config','data'):
    Path(runtime[key]).mkdir(parents=True,exist_ok=True)
archive = Path(runtime['archive']); archive.parent.mkdir()
shutil.copyfile('/rabbitr1/downloads/mtkclient-v2.1.4.1.tar.gz',archive)
for source,name in [(SCRIPTS/'prepare-mtkclient.py','prepare-mtkclient.py'),
                    (PROJECT/'mtkclient/transport.json','mtkclient-transport.json'),
                    (PROJECT/'patches/mtkclient/0001-guard-bulk-transfers.patch','mtkclient-transport.patch'),
                    (SCRIPTS/'prepare-flash.py','prepare-flash.py'),(SCRIPTS/'lk_handoff.py','lk_handoff.py')]:
    shutil.copyfile(source,out/name)
command = [sys.executable,str(out/'prepare-mtkclient.py'),'--workspace',str(workspace),
           '--manifest',str(out/'mtkclient-transport.json'),'--patch',str(out/'mtkclient-transport.patch'),
           '--archive',str(archive),'--destination',runtime['destination']]
# Inspect the environment used by the real patch subprocesses, then run them.
launcher = args.out/'check-git-workspace.py'
launcher.write_text('import runpy,subprocess,sys\n'+
    'real_run=subprocess.run\n'+
    'def checked(*args,**kwargs):\n'+
    ' assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == '+repr(str(workspace/'.gitconfig'))+'\n'+
    ' assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"\n'+
    ' return real_run(*args,**kwargs)\n'+
    'subprocess.run=checked\n'+
    'sys.argv=sys.argv[1:]\n'+
    'runpy.run_path(sys.argv[0],run_name="__main__")\n')
p = run([command[0],str(launcher),*command[1:]]); assert p.returncode == 0, p.stderr
assert json.loads(p.stdout)['files'] == 1164
note('patch subprocess uses Git configuration inside the selected workspace')
p = run(command+['--check']); assert p.returncode == 0, p.stderr
note('actual archive prepared and fully checked in explicit workspace')
require_failure(command,'already exists'); note('explicit workspace does not overwrite existing source')
require_failure(command[:-1]+[str(workspace/'src/mtkclient')], 'original mtkclient tree is protected')
note('original source protected within explicit workspace')
require_failure(command[:-1]+['/rabbitr1/outside-workspace'], 'path must be under')
note('source outside explicit workspace rejected')

# A recorder is the explicit fixture interpreter. It delegates only the two
# host checkers to Python. A target invocation is recorded and rejected, never run.
calls = args.out/'calls.jsonl'
interpreter = Path(runtime['python']); interpreter.parent.mkdir(parents=True)
interpreter.write_text('#!/usr/bin/env python3\n'+
    'import json,os,sys\nfrom pathlib import Path\n'+
    'args=sys.argv[1:]\n'+
    'with Path('+repr(str(calls))+').open("a") as f:f.write(json.dumps(args)+"\\n")\n'+
    'if args and args[0] in ("prepare-flash.py","prepare-mtkclient.py"):\n'+
    ' os.execv('+repr(sys.executable)+',['+repr(sys.executable)+']+args)\n'+
    'raise SystemExit(77)\n')
interpreter.chmod(0o755)
transport = flash.transport_metadata(out)
for restore in (False,True):
    name = 'restore' if restore else 'flash'
    script = flash.shell_script(out,'a',restore=restore,transport=transport,runtime=runtime)
    (out/(name+'.sh')).write_text(script)
    p=run(['bash','-n',str(out/(name+'.sh'))]);assert p.returncode==0,p.stderr
    # The remote path need not exist for local preview and cannot execute values.
    preview = flash.shell_script(out,'a',restore=restore,transport=transport,runtime=remote)
    preview_path=args.out/(name+'-remote-preview.sh');preview_path.write_text(preview)
    p=run(['bash',str(preview_path)],cwd=args.out);assert p.returncode==0,(p.stdout,p.stderr)
    assert 'Preview only.' in p.stdout and not calls.exists()
    assert '--expected-emmc-cid' not in p.stdout
    note(name+' remote preview uses no remote filesystem or interpreter')
    preview = flash.shell_script(out,'a',restore=restore,transport=transport,runtime=with_cid)
    preview_path=args.out/(name+'-cid-preview.sh');preview_path.write_text(preview)
    p=run(['bash',str(preview_path)],cwd=args.out);assert p.returncode==0,(p.stdout,p.stderr)
    suffix='restore' if restore else 'new'
    expected=['mtk.py w '+flash.partition_name(part,'a')+' '+part+'-'+suffix+
              '.img --parttype user --expected-emmc-cid '+expected_cid for part in flash.WRITE_PARTS]
    assert [line for line in p.stdout.splitlines() if line.startswith('mtk.py ')]==expected
    assert not calls.exists()
    note(name+' local preview includes normalized CID on every displayed write command')

(out/'runtime.json').write_text(json.dumps(runtime,indent=2)+'\n')
for part in flash.WRITE_PARTS:
    for suffix in ('new','restore'):
        with (out/(part+'-'+suffix+'.img')).open('wb') as f:f.truncate(flash.SIZES[part])
def sums():
    (out/'SHA256SUMS').write_text(''.join(flash.sha(p)+'  '+p.name+'\n' for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
sums()
for name in ('flash','restore'):
    calls.unlink(missing_ok=True)
    p=run(['bash',str(out/(name+'.sh')),'--write'],cwd=args.out)
    assert p.returncode==77,(p.stdout,p.stderr)
    seen=[json.loads(line) for line in calls.read_text().splitlines()]
    assert len(seen)==3 and seen[0]==['prepare-flash.py','check-runtime','runtime.json'],seen
    assert seen[1]==['prepare-mtkclient.py','--manifest','mtkclient-transport.json','--patch','mtkclient-transport.patch','--workspace',str(workspace),'--archive',str(archive),'--destination',runtime['destination'],'--check'],seen
    assert seen[2][0]==str(Path(runtime['destination'])/'mtk.py') and seen[2][1]=='gpt',seen
    assert seen[2][-2:]==['--expected-emmc-cid',expected_cid],seen
    assert not list(args.out.rglob('INJECTION'))
    note(name+' exact quoted runtime/checker argv; recorder blocks first target command')
# Exercise the generated run_mtk function for r/w too. The prefix contains
# the actual checks and function; stop before the generated partition preflight.
# The explicit interpreter fixture still refuses every target invocation.
text=(out/'flash.sh').read_text()
end=text.index('\n}',text.index('run_mtk() {'))+2
for command in [('r','boot_a','synthetic.img','--parttype','user'),
                ('w','boot_a','synthetic.img','--parttype','user')]:
    harness=args.out/('cid-'+command[0]+'.sh')
    harness.write_text(text[:end]+'\nrun_mtk '+shlex.join(command)+'\n')
    calls.unlink(missing_ok=True)
    p=run(['bash',str(harness),'--write'],cwd=args.out)
    assert p.returncode==77,(p.stdout,p.stderr)
    seen=[json.loads(line) for line in calls.read_text().splitlines()]
    assert len(seen)==3 and seen[-1]==[str(Path(runtime['destination'])/'mtk.py'),*command,'--expected-emmc-cid',expected_cid],seen
    note('generated '+command[0]+' invocation appends expected CID after command arguments')

# A standard venv link is permitted only at the interpreter leaf. Its target
# can be outside the runtime workspace; this fixture target still stays inside
# the task's local output directory. No target code is imported or executed.
base_interpreter = args.out/'base-interpreter-fixture'
interpreter.rename(base_interpreter); interpreter.symlink_to(base_interpreter)
p=run([sys.executable,str(out/'prepare-flash.py'),'check-runtime','runtime.json'],cwd=out)
assert p.returncode==0,p.stderr
interpreter.unlink();base_interpreter.rename(interpreter)
note('venv interpreter leaf may link outside runtime workspace; data/source exceptions unchanged')
# The literal runtime digest still rejects changed metadata if generic sums were
# recomputed. No checker/target process should run after this local gate fails.
saved=(out/'runtime.json').read_bytes();changed=dict(runtime);changed['cache']=str(workspace/'changed-cache')
(out/'runtime.json').write_text(json.dumps(changed,indent=2)+'\n');sums();calls.unlink()
p=run(['bash',str(out/'flash.sh'),'--write'],cwd=args.out)
assert p.returncode and not calls.exists();note('changed runtime binding rejected before host or target checker')
(out/'runtime.json').write_bytes(saved);sums()
# A validly hashed runtime with a symlink on disk is rejected by the actual
# runtime checker; the source checker/target recorder must remain unreachable.
cache=Path(runtime['cache']);moved=workspace/'saved-cache';cache.rename(moved);cache.symlink_to(moved,target_is_directory=True)
p=run(['bash',str(out/'flash.sh'),'--write'],cwd=args.out)
assert p.returncode and 'Runtime symlink path' in p.stderr
seen=[json.loads(line) for line in calls.read_text().splitlines()];assert len(seen)==1
cache.unlink();moved.rename(cache);note('runtime cache symlink rejected before source checker or target')
assert not any(x=='usb' or x.startswith('usb.') or x=='mtkclient' or x.startswith('mtkclient.') for x in sys.modules)
result={'status':'PASS','controls':len(controls),'names':controls,'device_imports_or_commands':False,
        'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [SCRIPTS/'prepare-mtkclient.py',SCRIPTS/'prepare-flash.py',Path(__file__)]}}
(args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
