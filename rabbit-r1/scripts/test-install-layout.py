#!/usr/bin/env python3
"""Bounded synthetic GPT/parser and real inspector CLI controls; no device I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

import r1_gpt as g

HERE = Path(__file__).resolve().parent
CAPACITY = 32 * 1024**3
checks = []


def sha(data):
    return hashlib.sha256(data).hexdigest()


def header_crc(raw):
    value = bytearray(raw)
    struct.pack_into('<I', value, 16, 0)
    struct.pack_into('<I', value, 16, zlib.crc32(value[:92]))
    return bytes(value)


def fixture(policy='nonzero', count=128):
    sizes = {'super': 8792064000, 'logo': 11 * 1024**2, 'para': 512 * 1024,
             'boot_para': 1024**2, 'md_udc': 23699456, 'userdata': 1024**3}
    for name, size in [('boot', 32 * 1024**2), ('dtbo', 8 * 1024**2),
                       ('vbmeta', 8 * 1024**2), ('lk', 1024**2)]:
        sizes.update({name + '_' + slot: size for slot in ('a', 'b')})
    table = bytearray(count * 128)
    start = 64
    for i, name in enumerate(g.REQUIRED):
        at = i * 128
        table[at:at+16] = bytes(range(1, 17))  # Synthetic type, not a captured GUID.
        table[at+16:at+32] = (i + 1).to_bytes(16, 'little')
        length = sizes[name] // 512
        struct.pack_into('<3Q', table, at+32, start, start+length-1, 0)
        encoded = name.encode('utf-16-le')
        table[at+56:at+56+len(encoded)] = encoded
        start += length
    last = CAPACITY // 512 - 1
    sectors = (len(table) + 511) // 512
    def header(current, alternate, array):
        value = bytearray(512)
        value[:8] = b'EFI PART'
        struct.pack_into('<4I4Q', value, 8, 0x10000, 92, 0, 0,
                         current, alternate, 64, last-sectors-1)
        value[56:72] = bytes(16) if policy == 'zero' else bytes(range(1, 17))
        struct.pack_into('<Q3I', value, 72, array, count, 128, zlib.crc32(table))
        return header_crc(value)
    padded = bytes(table).ljust(sectors * 512, b'\0')
    return [header(1, last, 2), padded, header(last, 1, last-sectors), padded]


def change_header(blobs, index, offset, fmt, value):
    raw = bytearray(blobs[index])
    struct.pack_into(fmt, raw, offset, value)
    blobs[index] = header_crc(raw)


def change_tables(blobs, edit):
    table = bytearray(blobs[1]); edit(table)
    blobs[1] = blobs[3] = bytes(table)
    count = struct.unpack_from('<I', blobs[0], 80)[0]
    for index in (0, 2):
        change_header(blobs, index, 88, '<I', zlib.crc32(table[:count*128]))


def accepted(name, blobs, **kwargs):
    pair = g.parse_pair(*blobs, user_bytes=CAPACITY, **kwargs)
    assert len(g.r1_regions(pair)) == 14
    checks.append({'name': name, 'result': 'accept'})
    return pair


def rejected(name, edit=lambda b: None, *, policy='zero', kwargs=None, message=None):
    blobs = fixture(policy); edit(blobs)
    args = {'user_bytes': CAPACITY, 'disk_guid_policy': policy}
    args.update(kwargs or {})
    try:
        g.r1_regions(g.parse_pair(*blobs, **args))
    except g.InvalidGPT as exc:
        if message is not None:
            assert str(exc) == message, (name, str(exc))
        checks.append({'name': name, 'result': 'reject', 'reason': str(exc)})
        return
    raise AssertionError('accepted ' + name)


def parser_controls():
    accepted('default nonzero profile', fixture())
    accepted('explicit zero profile', fixture('zero'), disk_guid_policy='zero')
    rejected('zero rejected by default', kwargs={'disk_guid_policy': 'nonzero'})
    rejected('nonzero rejected by zero profile', policy='nonzero', kwargs={'disk_guid_policy': 'zero'})
    for policy in ['both', '', None, True]:
        rejected('unknown policy ' + repr(policy), kwargs={'disk_guid_policy': policy})
    for index in (0, 2):
        rejected(f'mixed header policy {index}',
                 lambda b, i=index: change_header(b, i, 56, '<Q', 1))
        for name, at, fmt, value in [
            ('revision', 8, '<I', 0x20000), ('header size', 12, '<I', 96),
            ('reserved', 20, '<I', 1), ('physical current', 24, '<Q', 3),
            ('physical alternate', 32, '<Q', 7), ('first usable', 40, '<Q', 1),
            ('last usable', 48, '<Q', 2**64-1), ('array location', 72, '<Q', 0),
            ('count zero', 80, '<I', 0), ('count excessive', 80, '<I', 4097),
            ('entry stride', 84, '<I', 256)]:
            rejected(f'CRC-repaired header {index} {name}',
                     lambda b, i=index, a=at, f=fmt, v=value: change_header(b, i, a, f, v))
        rejected(f'header {index} CRC corruption',
                 lambda b, i=index: b.__setitem__(i, b[i][:16] + bytes(4) + b[i][20:]),
                 message='header CRC')
        rejected(f'header {index} truncated', lambda b, i=index: b.__setitem__(i, b[i][:-1]))
    rejected('primary array overlaps usable', lambda b: change_header(b, 0, 72, '<Q', 64))
    rejected('backup array overlaps final header',
             lambda b: change_header(b, 2, 72, '<Q', CAPACITY//512-2))
    rejected('paired usable metadata differs', lambda b: change_header(b, 2, 40, '<Q', 65),
             message='headers disagree')
    for name, edit, reason in [
        ('zero unique GUID', lambda t: t.__setitem__(slice(16, 32), bytes(16)), 'zero partition GUID'),
        ('duplicate unique GUID', lambda t: t.__setitem__(slice(144, 160), t[16:32]), 'duplicate name or partition GUID'),
        ('zero type with populated entry', lambda t: t.__setitem__(slice(0, 16), bytes(16)), 'nonzero unused entry'),
        ('overlap', lambda t: struct.pack_into('<Q', t, 160, 64), 'overlapping partitions'),
        ('partition outside usable', lambda t: struct.pack_into('<Q', t, 32, 2), 'partition bounds'),
        ('duplicate casefolded name', lambda t: t.__setitem__(slice(184, 256), b'S\0U\0P\0E\0R\0'.ljust(72, b'\0')), 'duplicate name or partition GUID'),
        ('invalid UTF16', lambda t: t.__setitem__(slice(56, 58), b'\x00\xd8'), 'invalid name encoding'),
        ('unsupported target attributes', lambda t: struct.pack_into('<Q', t, 48, 1), 'unsupported r1 attributes: super'),
        ('wrong required size', lambda t: struct.pack_into('<Q', t, 40, 65), 'unsupported r1 size: super'),
        ('missing required name', lambda t: t.__setitem__(slice(0, 128), bytes(128)), 'missing partition: super')]:
        rejected('CRC-repaired entry ' + name, lambda b, edit=edit: change_tables(b, edit), message=reason)
    for index in (1, 3):
        rejected(f'array {index} CRC corruption', lambda b, i=index: b.__setitem__(i, bytes(1)+b[i][1:]), message='array CRC')
        rejected(f'array {index} short', lambda b, i=index: b.__setitem__(i, b[i][:-1]))
        rejected(f'array {index} long', lambda b, i=index: b.__setitem__(i, b[i]+bytes(1)))
    # Fixed synthetic CRC32 collision: enforce byte equality even when CRCs agree.
    seen = {}
    for i in range(200000):
        x = hashlib.sha256(str(i).encode()).digest()[:8]; crc = zlib.crc32(x)
        if crc in seen and seen[crc] != x:
            collision = (seen[crc], x); break
        seen[crc] = x
    else:
        raise AssertionError('bounded collision fixture missing')
    def collision_edit(b):
        for i, data in zip((1, 3), collision):
            table = bytearray(b[i]); table[48:56] = data; b[i] = bytes(table)
        assert b[1] != b[3] and zlib.crc32(b[1]) == zlib.crc32(b[3])
        for i in (0, 2): change_header(b, i, 88, '<I', zlib.crc32(b[1]))
    rejected('different arrays with matching CRC32', collision_edit, message='partition arrays differ')
    b = fixture('zero', count=127)
    accepted('bounded partial array sector', b, disk_guid_policy='zero')
    b[1] = b[1][:-1] + b'\x01'
    try: g.parse_pair(*b, user_bytes=CAPACITY, disk_guid_policy='zero')
    except g.InvalidGPT as exc: assert str(exc) == 'nonzero array sector padding'
    else: raise AssertionError('nonzero array padding accepted')
    checks.append({'name': 'partial-sector nonzero padding', 'result': 'reject'})


def cli_controls(directory):
    names = ['primary-header', 'primary-array', 'backup-header', 'backup-array']
    for name, blob in zip(names, fixture('zero')): (directory/name).write_bytes(blob)
    identity = {'schema': 1, 'chip': dict(zip(['hw_code', 'hw_sub_code', 'hw_version', 'sw_version', 'chip_evolution'], [1894, 1, 2, 0, 0])),
                'cid': '0123456789abcdef0123456789abcdef', 'emmc_type': 1,
                'sector_bytes': 512, 'fwver': 0,
                'sizes': dict(zip(['boot1', 'boot2', 'rpmb', 'gp1', 'gp2', 'gp3', 'gp4', 'user'], [4194304, 4194304, 16777216, 0, 0, 0, 0, CAPACITY]))}
    raw = (json.dumps(identity) + '\n').encode()
    (directory/'observed.json').write_bytes(raw); (directory/'expected.json').write_bytes(raw)
    common = [sys.executable, str(HERE/'check-install-layout.py')]
    for name in names: common += ['--'+name, str(directory/name)]
    common += ['--observation', str(directory/'observed.json'), '--observation-sha256', sha(raw),
               '--expected-observation', str(directory/'expected.json'), '--expected-observation-sha256', sha(raw),
               '--user-bytes', str(CAPACITY)]
    def run(name, extra=(), accept=False, preexisting=False):
        out = directory/(name+'.json')
        result = subprocess.run(common + list(extra) + ['--out', str(out)], capture_output=True, text=True)
        assert (result.returncode == 0) == accept, (name, result.stdout, result.stderr)
        if not accept and not preexisting: assert not out.exists()
        checks.append({'name': 'CLI ' + name, 'result': 'accept' if accept else 'reject'})
        return out
    out = run('zero', ['--disk-guid-policy', 'zero'], accept=True)
    report = json.loads(out.read_text());binding=report['binding']
    assert report['scope']=='offline layout consistency' and report['partition_count']==14
    assert binding['parser']['sha256']==sha((HERE/'r1_gpt.py').read_bytes())
    assert binding['observation']['sha256']==binding['expected_observation']['sha256']==sha(raw)
    assert set(binding['inputs'])=={n.replace('-', '_') for n in names}
    assert all(binding['inputs'][n.replace('-', '_')]['sha256']==sha((directory/n).read_bytes()) for n in names)
    assert report['binding_sha256']==sha(json.dumps(binding,sort_keys=True,separators=(',', ':')).encode())
    assert out.stat().st_mode & 0o777 == 0o600
    run('default');run('bad-pin',['--disk-guid-policy','zero','--expected-observation-sha256','0'*64])
    run('wrong-capacity',['--disk-guid-policy','zero','--user-bytes',str(CAPACITY-512)])
    for name, edit in [('changed-cid',lambda x:x.update(cid='1123456789abcdef0123456789abcdef')),
                       ('blank-cid',lambda x:x.update(cid='0'*32)),
                       ('changed-chip',lambda x:x['chip'].update(hw_code=1895)),
                       ('changed-geometry',lambda x:x['sizes'].update(boot1=512)),
                       ('boolean-sector',lambda x:x.update(sector_bytes=True))]:
        changed=json.loads(raw);edit(changed);data=json.dumps(changed).encode();p=directory/'altered.json';p.write_bytes(data)
        run(name,['--disk-guid-policy','zero','--observation',str(p),'--observation-sha256',sha(data)])
    p=directory/'duplicate.json';p.write_bytes(raw.rstrip()[:-1]+b',"schema":1}')
    run('duplicate-key',['--disk-guid-policy','zero','--observation',str(p),'--observation-sha256',sha(p.read_bytes())])
    link=directory/'linked-header';link.symlink_to(directory/'primary-header')
    run('symlink-input',['--disk-guid-policy','zero','--primary-header',str(link)])
    before=out.read_bytes();run('zero',['--disk-guid-policy','zero'],preexisting=True);assert out.read_bytes()==before
    copied = directory/'modified-source';copied.mkdir()
    helper = copied/'check-install-layout.py'
    helper.write_bytes((HERE/helper.name).read_bytes())
    marker = copied/'executed'
    injected = ('\nfrom pathlib import Path\nPath(' + repr(str(marker))
                + ').write_text("unexpected execution")\n').encode()
    (copied/'r1_gpt.py').write_bytes((HERE/'r1_gpt.py').read_bytes() + injected)
    refused = copied/'report.json'
    result = subprocess.run([common[0], str(helper), *common[2:],
                             '--disk-guid-policy', 'zero', '--out', str(refused)],
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'input SHA256 differs from supplied pin' in result.stderr
    assert not marker.exists() and not refused.exists()
    checks.append({'name': 'CLI modified parser refused before execution', 'result': 'reject'})
    assert not list(directory.glob('.*.json.*'))


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args()
    assert a.out.is_absolute() and a.out.is_relative_to('/rabbitr1') and '..' not in a.out.parts
    a.out.mkdir()
    parser_controls()
    with tempfile.TemporaryDirectory(prefix='layout-',dir='/rabbitr1/.tmp') as temp:cli_controls(Path(temp))
    result={'status':'pass','scope':'synthetic offline parser and inspector CLI','checks_passed':len(checks),'checks':checks,
            'sources':{n:sha((HERE/n).read_bytes()) for n in ['r1_gpt.py','check-install-layout.py','test-install-layout.py']}}
    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':'pass','checks_passed':len(checks)}))


if __name__=='__main__':main()
