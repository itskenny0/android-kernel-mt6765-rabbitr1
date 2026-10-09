"""Exercise real reviewed LK prefixes with synthetic complete slot originals."""
import argparse
import dataclasses
import os
import subprocess
from unittest import mock
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = Path('/rabbitr1')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--stock', type=Path, required=True)
parser.add_argument('--patched', type=Path, required=True)
parser.add_argument('--build-record', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
for path in (args.stock, args.patched, args.build_record, args.out):
    path = path.absolute()
    if not path.is_relative_to(ROOT) or path.resolve() != path:
        parser.error('Require direct paths under /rabbitr1')
WORK = args.out.absolute()
if os.path.lexists(WORK):
    parser.error('Refusing preexisting output')
WORK.mkdir()
spec = importlib.util.spec_from_file_location('lk_slot_preparation', HERE/'lk_slots.py')
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
spec.loader.exec_module(m)
stock = args.stock.read_bytes()
patched = args.patched.read_bytes()
assert len(stock) == len(patched) == m.PAYLOAD_BYTES
assert hashlib.sha256(stock).hexdigest() == m.STOCK_SHA256
assert hashlib.sha256(patched).hexdigest() == m.PATCHED_SHA256
build_record = json.loads(args.build_record.read_text())
assert build_record['stock_lk_sha256'] == m.STOCK_SHA256
assert build_record['stock_lk_bytes'] == m.PAYLOAD_BYTES
assert build_record['files']['lk.bin'] == {'bytes': m.PAYLOAD_BYTES, 'sha256': m.PATCHED_SHA256}
tail_size = m.PARTITION_BYTES - m.PAYLOAD_BYTES
tail_a = bytes((i * 37 + i // 251) % 256 for i in range(tail_size))
tail_b = bytes((i * 19 + i // 257 + 3) % 256 for i in range(tail_size))
assert tail_a != tail_b and len(set(tail_a)) == len(set(tail_b)) == 256
variants_a = {'stock':stock+tail_a, 'current':patched+tail_a,
              'zero':bytes(m.PARTITION_BYTES), 'ff':b'\xff'*m.PARTITION_BYTES}
variants_b = {'stock':stock+tail_b, 'current':patched+tail_b,
              'zero':bytes(m.PARTITION_BYTES), 'ff':b'\xff'*m.PARTITION_BYTES}
passed = []


def rejects(label, function):
    try:
        function()
    except m.InvalidLK:
        passed.append(label)
    else:
        raise AssertionError('Accepted '+label)


for a, b in itertools.product(variants_a, variants_b):
    original_a, original_b = variants_a[a], variants_b[b]
    original_hashes = (m.sha(original_a), m.sha(original_b))
    label = a+'-'+b
    if a in ('zero','ff') and b in ('zero','ff'):
        rejects('both-unrecognized-'+label, lambda:m.prepare(original_a, original_b, patched))
        continue
    prepared = m.prepare(original_a, original_b, patched)
    assert m.validate(prepared, original_a, original_b, patched)
    assert tuple(row[0] for row in prepared.images) == ('lk_a','lk_b')
    for index, original in enumerate((original_a, original_b)):
        result = prepared.images[index][1]
        assert len(result) == m.PARTITION_BYTES
        assert result[:m.PAYLOAD_BYTES] == patched
        assert result[m.PAYLOAD_BYTES:] == original[m.PAYLOAD_BYTES:]
        assert prepared.originals[index][1] == original_hashes[index]
    assert original_hashes == (m.sha(original_a), m.sha(original_b))
    passed.append('preserve-both-originals-'+label)

original_a, original_b = variants_a['stock'], variants_b['stock']
prepared = m.prepare(original_a, original_b, patched)
assert prepared.images[0][1] != prepared.images[1][1]
for slot in (0,1):
    for offset in (0, m.PAYLOAD_BYTES-1, m.PAYLOAD_BYTES,
                   m.PAYLOAD_BYTES+12345, m.PARTITION_BYTES-1):
        changed = bytearray(prepared.images[slot][1]); changed[offset] ^= 1
        rows = list(prepared.images); rows[slot] = (m.SLOTS[slot], bytes(changed))
        candidate = dataclasses.replace(prepared, images=tuple(rows))
        rejects(f'changed-slot-{slot}-byte-{offset}',
                lambda:m.validate(candidate, original_a, original_b, patched))

for offset in (0, 19012, m.PAYLOAD_BYTES-1):
    changed = bytearray(original_b); changed[offset] ^= 1
    rejects('unknown-original-prefix-'+str(offset),
            lambda:m.prepare(original_a, bytes(changed), patched))
for blank in (0,255):
    changed = bytearray([blank])*m.PARTITION_BYTES; changed[-1] ^= 1
    rejects('mixed-blank-original-'+str(blank),
            lambda:m.prepare(original_a, bytes(changed), patched))
for label, candidate in [('stock-as-patched',stock),('short-patched',patched[:-1]),
                         ('long-patched',patched+b'\0'),('mutable-patched',bytearray(patched))]:
    rejects(label, lambda:m.prepare(original_a, original_b, candidate))
for label, original in [('short-original',original_b[:-1]),
                        ('long-original',original_b+b'\0'),
                        ('mutable-original',bytearray(original_b))]:
    rejects(label, lambda:m.prepare(original_a, original, patched))
changed = bytearray(original_b); changed[-1] ^= 1
rejects('backup-tail-changed-after-preparation',
        lambda:m.validate(prepared, original_a, bytes(changed), patched))
rejects('backup-slots-swapped', lambda:m.validate(prepared, original_b, original_a, patched))
for label, candidate in [
    ('images-swapped', dataclasses.replace(prepared, images=tuple(reversed(prepared.images)))),
    ('tail-from-other-slot', dataclasses.replace(prepared, images=(prepared.images[0],('lk_b',prepared.images[0][1])))),
    ('missing-b', dataclasses.replace(prepared, images=prepared.images[:1])),
    ('mutable-image', dataclasses.replace(prepared, images=(('lk_a',bytearray(prepared.images[0][1])),prepared.images[1]))),
    ('changed-original-pin', dataclasses.replace(prepared, originals=(('lk_a','0'*64),prepared.originals[1]))),
    ('changed-profile', dataclasses.replace(prepared, profiles=(('lk_a','current-patched'),prepared.profiles[1]))),
]:
    rejects(label, lambda:m.validate(candidate, original_a, original_b, patched))

class AlwaysEqual:
    def __eq__(self, other):
        return True


class FalseString(str):
    def __eq__(self, other):
        return True


class EqualityMustNotRun:
    def __eq__(self, other):
        raise AssertionError('Untrusted binding equality executed')


for field in ('originals', 'profiles'):
    for label, value in [('opaque-equality',AlwaysEqual()),
                         ('list',list(getattr(prepared,field))),
                         ('empty',()), ('extra-row',getattr(prepared,field)*2),
                         ('no-equality-execution',EqualityMustNotRun()),
                         ('wrong-string-subclass',(('lk_a',FalseString('incorrect')),getattr(prepared,field)[1])),
                         ('wrong-tag-subclass',((FalseString('wrong-slot'),getattr(prepared,field)[0][1]),getattr(prepared,field)[1]))]:
        candidate = dataclasses.replace(prepared, **{field:value})
        rejects(field+'-'+label, lambda:m.validate(candidate, original_a, original_b, patched))
candidate = dataclasses.replace(prepared, images=((FalseString('wrong-slot'),prepared.images[0][1]),prepared.images[1]))
rejects('wrong-image-tag-subclass', lambda:m.validate(candidate, original_a, original_b, patched))

# Same prepared bytes do not prove that an earlier full original is current.
rejects('stale-original-prefix-identical-result',
        lambda:m.validate(prepared, patched+tail_a, original_b, patched))
assert m.prepare(patched+tail_a, patched+tail_b, patched).images == (
    ('lk_a',patched+tail_a),('lk_b',patched+tail_b))
passed.append('current-pair-idempotent')
assert {i for i,(x,y) in enumerate(zip(original_a,prepared.images[0][1])) if x!=y} == {
    i for i,(x,y) in enumerate(zip(stock,patched)) if x!=y}
passed.append('only-reviewed-prefix-differences')

cli_path = HERE/'prepare-lk-slots.py'
spec = importlib.util.spec_from_file_location('lk_slots_cli', cli_path)
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
inputs = WORK/'inputs'; inputs.mkdir()
pa,pb,pp = (inputs/name for name in ('lk_a.img','lk_b.img','lk.bin'))
for path,data in ((pa,original_a),(pb,original_b),(pp,patched)):path.write_bytes(data)
input_hashes={p:m.sha(p.read_bytes()) for p in (pa,pb,pp)}

def command(out, *extra, **changes):
    values={'--lk-a':pa,'--lk-b':pb,'--patched-lk':pp,'--out':out}
    values.update(changes)
    return [sys.executable,str(cli_path)]+[str(v) for pair in values.items() for v in pair]+list(extra)


def run_cli(out, valid=True, *extra, **changes):
    result=subprocess.run(command(out,*extra,**changes),cwd=WORK,capture_output=True,text=True)
    assert (result.returncode==0)==valid, result.stderr
    if valid:return json.loads(result.stdout)
    assert 'LK preparation failed:' in result.stderr or 'error:' in result.stderr
    return result


def reject_files(label, function):
    try:function()
    except (ValueError,OSError):passed.append(label)
    else:raise AssertionError('Accepted '+label)

out=WORK/'valid';summary=run_cli(out)
assert {p.name for p in out.iterdir()}=={'lk_a.img','lk_b.img','preparation.json'}
record=json.loads((out/'preparation.json').read_text())
assert record['images']==summary['images']
for slot,original in (('lk_a',original_a),('lk_b',original_b)):
    data=(out/(slot+'.img')).read_bytes()
    assert data==patched+original[m.PAYLOAD_BYTES:]
    assert record['originals'][slot]['sha256']==m.sha(original)
    assert record['images'][slot]['sha256']==m.sha(data)
    assert record['images'][slot]['original_sha256']==m.sha(original)
assert all(m.sha(p.read_bytes())==h for p,h in input_hashes.items())
passed.append('actual-cli-complete-pair-record-inputs-preserved')
before={p.name:m.sha(p.read_bytes()) for p in out.iterdir()};run_cli(out,False)
assert {p.name:m.sha(p.read_bytes()) for p in out.iterdir()}==before
passed.append('cli-preexisting-output-preserved')
for name,value in [('short',original_b[:-1]),('unknown',b'x'*m.PARTITION_BYTES)]:
    q=inputs/name;q.write_bytes(value);dest=WORK/('reject-'+name)
    run_cli(dest,False,**{'--lk-b':q});assert not dest.exists();passed.append('cli-'+name+'-before-output')
q=inputs/'stock-as-patched';q.write_bytes(stock);dest=WORK/'reject-stock-payload';run_cli(dest,False,**{'--patched-lk':q});assert not dest.exists();passed.append('cli-rejects-stock-as-patched')
link=inputs/'link';link.symlink_to(pb);dest=WORK/'reject-link';run_cli(dest,False,**{'--lk-b':link});assert not dest.exists();passed.append('cli-symlink-input-rejected')
fifo=inputs/'fifo';os.mkfifo(fifo);dest=WORK/'reject-fifo';run_cli(dest,False,**{'--lk-b':fifo});assert not dest.exists();passed.append('cli-nonregular-input-rejected')
linkout=WORK/'output-link';linkout.symlink_to(out,target_is_directory=True);run_cli(linkout,False);assert before=={p.name:m.sha(p.read_bytes()) for p in out.iterdir()};passed.append('cli-symlink-output-rejected')
# Rejection occurs before touching either external path.
run_cli(Path('/outside-r1-lk-test-output'),False);passed.append('cli-outside-output-rejected')
run_cli(WORK/'outside-input',False,**{'--lk-a':Path('/outside-r1-lk-test-input')});passed.append('cli-outside-input-rejected')
run_cli(WORK/'unsupported-action',False,'--write');assert not (WORK/'unsupported-action').exists();passed.append('cli-no-write-action')
# One uniformly blank full original is supported alongside a known prefix.
blank=inputs/'blank';blank.write_bytes(b'\xff'*m.PARTITION_BYTES);blank_out=WORK/'one-blank';run_cli(blank_out,**{'--lk-b':blank})
assert (blank_out/'lk_b.img').read_bytes()==patched+b'\xff'*tail_size
assert json.loads((blank_out/'preparation.json').read_text())['originals']['lk_b']['profile']=='blank-ff';passed.append('cli-uniform-blank-tail-preserved')
zero=inputs/'zero';zero.write_bytes(bytes(m.PARTITION_BYTES));dest=WORK/'both-blank';run_cli(dest,False,**{'--lk-a':zero,'--lk-b':blank});assert not dest.exists();passed.append('cli-both-blank-rejected')
# Exercise actual file-layer failure paths without changing the pure module.
write=cli.write_file
for failure in ('short','raise','change-original','collateral-output'):
    dest=WORK/('injected-'+failure)
    def injected(path,data):
        if path.name=='lk_b.img':
            if failure=='short':return write(path,data[:-1])
            if failure=='raise':raise OSError('modeled output I/O failure')
            if failure=='change-original':pb.write_bytes(patched+tail_b)
            if failure=='collateral-output':
                changed=bytearray((path.parent/'lk_a.img').read_bytes());changed[-1]^=1
                (path.parent/'lk_a.img').write_bytes(changed)
        return write(path,data)
    with mock.patch.object(cli,'write_file',side_effect=injected):
        reject_files('output-failure-'+failure,lambda:cli.prepare_files(pa,pb,pp,dest))
    assert not (dest/'preparation.json').exists()
    if failure!='collateral-output':assert (dest/'lk_a.img').read_bytes()==patched+tail_a
    pb.write_bytes(original_b)
# O_EXCL preserves a file appearing inside the new output directory.
def collide(path,data):
    if path.name=='lk_b.img':path.write_bytes(b'new-owner')
    return write(path,data)
dest=WORK/'injected-collision'
with mock.patch.object(cli,'write_file',side_effect=collide):
    reject_files('output-publication-collision',lambda:cli.prepare_files(pa,pb,pp,dest))
assert (dest/'lk_b.img').read_bytes()==b'new-owner' and not (dest/'preparation.json').exists()
# Initial read of three files completed, then an input changed before the
# second observation: fail before creating any output directory.
read=cli.read_input;calls=0

def change_before_second(path,size):
    global calls
    calls+=1
    if calls==4:pb.write_bytes(patched+tail_b)
    return read(path,size)
dest=WORK/'injected-preparation-race'
with mock.patch.object(cli,'read_input',side_effect=change_before_second):
    reject_files('input-change-before-output',lambda:cli.prepare_files(pa,pb,pp,dest))
assert not dest.exists();pb.write_bytes(original_b)
assert all(m.sha(p.read_bytes())==h for p,h in input_hashes.items())
result={'status':'pass','controls':len(passed),'cases':passed,
        'scope':'Actual pinned stock/current prefixes with synthetic full originals and file-only CLI; no device, installer admission or physical freshness proof',
        'module_sha256':m.sha((HERE/'lk_slots.py').read_bytes()),
        'cli_sha256':m.sha(cli_path.read_bytes()),'runner_sha256':m.sha(Path(__file__).read_bytes()),
        'stock_prefix_sha256':m.sha(stock),'patched_prefix_sha256':m.sha(patched),
        'build_record_sha256':m.sha(args.build_record.read_bytes()),
        'physical_device_accessed':False,'complete_package_prepared':False}
(WORK/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print('PASS',len(passed),'LK slot API and offline file controls')
