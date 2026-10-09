#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build and verify an offline r1 super image containing both slots."""
import argparse, hashlib, json, os, re, shutil, stat, struct, subprocess
from pathlib import Path

ROOT = Path('/rabbitr1')
WORK = ROOT / 'out/android-super'
PARTS = ('system', 'system_ext', 'product', 'vendor')
LAYOUT = {'super_bytes': 8792064000, 'group_bytes': 4294967296,
          'metadata_bytes': 65536, 'metadata_slots': 3, 'block_bytes': 4096,
          'alignment_bytes': 1048576}
CHECKS = (
    'completed build and staged provenance',
    'image hashes and logical partition budgets',
    'actual check-all-partition-sizes result',
    'full vbmeta signature, descriptors and image correspondence',
    'sparse super metadata, A/B layout and actual extent payloads',
    'actual ext4 contents: init, fstab, VINTF, services, app and modules',
    'filesystem pstore and allocator payloads match committed target audit',
    'packaged ARM64 services and charging APK metadata',
    'vendor pstore module kernel match and explicit-load packaging',
    'image immutability during read-only audit',
)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def local(value, output=False):
    p = Path(value).absolute()
    require(p.resolve() == p and p.is_relative_to(WORK if output else ROOT),
            'Path outside allowed tree or through symlink: ' + str(p))
    return p


def identity(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def digest_file(value):
    p = local(value)
    require(stat.S_ISREG(p.lstat().st_mode), 'Not a regular input: ' + str(p))
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        before = os.fstat(f.fileno())
        require(stat.S_ISREG(before.st_mode), 'Not a regular input: ' + str(p))
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
        require(identity(before) == identity(os.fstat(f.fileno())) == identity(p.stat()),
                'Input changed during hash: ' + str(p))
    return {'path': str(p), 'bytes': before.st_size, 'sha256': digest}


def checked_record(row):
    require(isinstance(row, dict) and set(('path', 'bytes', 'sha256')) <= row.keys(),
            'Incomplete pinned file record')
    got = digest_file(row['path'])
    require(all(got[k] == row[k] for k in got), 'Pinned input changed: ' + row['path'])
    return got


def read_audit(path, expected):
    # All completion gates run before any logical filesystem image is opened.
    pin = digest_file(path)
    require(pin['sha256'] == expected, 'Audit SHA256 mismatch')
    data = Path(pin['path']).read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, 'Audit changed before parsing')
    audit = json.loads(data)
    require(audit.get('mode') == 'verify' and audit.get('overall') == 'pass'
            and audit.get('errors') == [], 'A completed passing image audit is required')
    rows = audit.get('checks', [])
    require(len(rows) == len(CHECKS) and {r.get('name') for r in rows} == set(CHECKS)
            and all(r.get('status') == 'pass' for r in rows), 'Incomplete/failed audit checks')
    checks = {r['name']: r['details'] for r in rows}
    provenance = checks[CHECKS[0]]
    commit = audit.get('build_start_source_commit', '')
    require(re.fullmatch('[0-9a-f]{40}', commit)
            and provenance.get('build_start_commit') == commit, 'Build revision mismatch')
    checked_record(audit['verifier'])
    log = checked_record(provenance['build_log'])
    require('#### build completed successfully' in Path(log['path']).read_text(errors='replace'),
            'Successful pinned build log missing')
    require(checks[CHECKS[-1]].get('image_sizes_and_mtimes_unchanged') is True,
            'Audit did not finish image immutability check')
    return pin, audit, checks


def make_plan(audit_path, audit_sha256, tool_path, tool_sha256, layout=None):
    # Alternate dimensions exist only for imported synthetic tests; the CLI fixes r1.
    layout = dict(LAYOUT if layout is None else layout)
    pin, audit, checks = read_audit(audit_path, audit_sha256)
    tool = digest_file(tool_path)
    require(tool['sha256'] == tool_sha256 and os.access(tool['path'], os.X_OK),
            'Pinned executable lpmake missing/changed')
    sizes = checks[CHECKS[1]]
    images = {}
    for name in PARTS:
        row = sizes[name]
        require(row.get('android_sparse') is False and row.get('logical_bytes') == row.get('bytes'),
                'Expected complete raw filesystem image: ' + name)
        size = row['bytes']
        require(type(size) is int and 0 < size <= layout['group_bytes']
                and size % layout['block_bytes'] == 0, 'Invalid complete image size: ' + name)
        images[name] = checked_record(row)
        with Path(images[name]['path']).open('rb') as f:
            require(f.read(4) != bytes.fromhex('3aff26ed'), 'Sparse file mislabeled raw')
    total = sum(row['bytes'] for row in images.values())
    require(total <= layout['group_bytes'], 'Both-slot per-group payload exceeds limit')
    align = layout['alignment_bytes']
    first = ((12288 + 2 * layout['metadata_slots'] * layout['metadata_bytes'] + align - 1)
             // align) * align
    # Conservative initial allocation bound, independently checked again in output.
    bound = first + 2 * sum(((row['bytes'] + align - 1) // align) * align for row in images.values())
    require(bound <= layout['super_bytes'], 'Insufficient metadata/alignment headroom')
    return {'schema': 1, 'status': 'verified-input-plan-only', 'audit': pin,
            'build_start_source_commit': audit['build_start_source_commit'],
            'lpmake': tool, 'layout': layout, 'images': images,
            'group_used_bytes': total, 'conservative_allocation_end': bound,
            'device_access': False, 'populates_slots': ['a', 'b'],
            'source_factory_super_used_as_payload': False}


def command(plan, output):
    d = plan['layout']
    argv = [plan['lpmake']['path'], '--metadata-size', str(d['metadata_bytes']),
            '--metadata-slots', str(d['metadata_slots']), '--super-name', 'super',
            '--block-size', str(d['block_bytes']), '--device',
            'super:%d:%d:0' % (d['super_bytes'], d['alignment_bytes'])]
    for slot in 'ab':
        argv += ['--group', 'r1_dynamic_partitions_%s:%d' % (slot, d['group_bytes'])]
    for name in PARTS:
        for slot in 'ab':
            full = name + '_' + slot
            argv += ['--partition', '%s:none:%d:r1_dynamic_partitions_%s' %
                     (full, plan['images'][name]['bytes'], slot),
                     '--image', full + '=' + plan['images'][name]['path']]
    return argv + ['--output', str(output)]  # No --sparse: raw file, never a DA write.


def name(raw):
    text, sep, rest = raw.partition(b'\0')
    require(sep and not any(rest) and re.fullmatch(b'[A-Za-z0-9_]+', text), 'Invalid LP name')
    return text.decode('ascii')


def parse_metadata(blob):
    magic, major, minor, hs = struct.unpack_from('<IHHI', blob)
    require(magic == 0x414c5030 and major == 10 and minor in (0, 1, 2)
            and hs == (256 if minor == 2 else 128), 'LP header version/size')
    h = bytearray(blob[:hs]); wanted = bytes(h[12:44]); h[12:44] = bytes(32)
    require(hashlib.sha256(h).digest() == wanted, 'LP header checksum')
    if hs == 256:
        require(not any(blob[128:256]), 'LP flags/reserved bytes, including virtual A/B')
    ts = struct.unpack_from('<I', blob, 44)[0]
    require(hs + ts <= len(blob), 'LP tables out of bounds')
    table = blob[hs:hs+ts]
    require(hashlib.sha256(table).digest() == blob[48:80], 'LP table checksum')
    require(not any(blob[hs+ts:]), 'Unexpected metadata reservation bytes')
    spans = []
    def rows(at, fmt):
        off, count, stride = struct.unpack_from('<III', blob, at)
        require(stride == struct.calcsize(fmt) and off + count * stride <= len(table), 'LP table bounds/stride')
        spans.append((off, off + count * stride))
        return [struct.unpack_from(fmt, table, off + i * stride) for i in range(count)]
    ps = rows(80, '<36sIIII'); es = rows(92, '<QIQI')
    gs = rows(104, '<36sIQ'); ds = rows(116, '<QIIQ36sI')
    spans.sort()
    require(spans[0][0] == 0 and spans[-1][1] == ts
            and all(a[1] == b[0] for a, b in zip(spans, spans[1:])), 'LP table coverage/overlap')
    return ps, es, gs, ds


def validate_raw(path, plan):
    path = local(path); d = plan['layout']; size = d['super_bytes']
    before = identity(path.stat())
    require(stat.S_ISREG(path.stat().st_mode) and path.stat().st_size == size, 'Raw super physical size/type')
    with path.open('rb') as f:
        def read(at, count):
            require(0 <= at <= size and 0 <= count <= size-at, 'Raw read out of bounds')
            f.seek(at); data = f.read(count); require(len(data) == count, 'Short raw super read'); return data
        require(read(0, 4096) == bytes(4096), 'Reserved initial block must be zero; sparse output rejected')
        geometry = read(4096, 4096)
        require(geometry == read(8192, 4096), 'Geometry copies differ')
        magic, gs = struct.unpack_from('<II', geometry)
        require((magic, gs) == (0x616c4467, 52), 'LP geometry format')
        h = bytearray(geometry[:gs]); wanted = bytes(h[8:40]); h[8:40] = bytes(32)
        require(hashlib.sha256(h).digest() == wanted and not any(geometry[gs:]), 'Geometry checksum/padding')
        require(struct.unpack_from('<III', geometry, 40) ==
                (d['metadata_bytes'], d['metadata_slots'], d['block_bytes']), 'Geometry dimensions')
        copies = [read(12288 + i*d['metadata_bytes'], d['metadata_bytes']) for i in range(2*d['metadata_slots'])]
        require(all(b == copies[0] for b in copies), 'All primary/backup metadata slots must match')
        ps, es, gs, ds = parse_metadata(copies[0])
        groups = {name(n): (flags, maximum) for n, flags, maximum in gs}
        require(len(gs) == 3 and groups == {'default': (0, 0),
                'r1_dynamic_partitions_a': (0, d['group_bytes']),
                'r1_dynamic_partitions_b': (0, d['group_bytes'])}, 'LP group definitions')
        require(len(ds) == 1, 'Expected a single physical super device')
        first, alignment, offset, dev_size, dev_name, flags = ds[0]
        end_metadata = 12288 + 2*d['metadata_slots']*d['metadata_bytes']
        require(name(dev_name) == 'super' and dev_size == size and flags == 0
                and alignment == d['alignment_bytes'] and offset == 0
                and end_metadata <= first*512 < size and first*512 % alignment == 0, 'LP device geometry')
        expected = {p+'_'+s for p in PARTS for s in 'ab'}
        require(len(ps) == 8 and {name(p[0]) for p in ps} == expected, 'Both full slot partition sets required')
        ranges = []; hashes = {}; used = {'a': 0, 'b': 0}; claimed = []
        for raw, attributes, begin, count, group in ps:
            n = name(raw); base = n[:-2]; slot = n[-1]
            require(attributes == 0 and group < len(gs) and name(gs[group][0]) == 'r1_dynamic_partitions_'+slot,
                    'LP partition group/attributes')
            require(count > 0 and begin+count <= len(es), 'Both slots need populated extents')
            h = hashlib.sha256(); length = 0
            for index in range(begin, begin+count):
                claimed.append(index); sectors, kind, sector, source = es[index]
                at, num = sector*512, sectors*512
                require(kind == source == 0 and num > 0 and at >= first*512 and at+num <= size
                        and at % d['block_bytes'] == num % d['block_bytes'] == 0, 'LP extent bounds/type/alignment')
                ranges.append((at, at+num, n)); length += num
                for off in range(0, num, 1024*1024):
                    h.update(read(at+off, min(num-off, 1024*1024)))
            require(length == plan['images'][base]['bytes'], 'Complete logical image size mismatch: '+n)
            require(h.hexdigest() == plan['images'][base]['sha256'], 'Payload hash mismatch: '+n)
            hashes[n] = h.hexdigest(); used[slot] += length
        require(sorted(claimed) == list(range(len(es))), 'Unassigned/reused LP extent records')
        ranges.sort()
        require(all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])), 'Physical extents overlap')
        require(all(0 < n <= d['group_bytes'] for n in used.values()), 'Group payload budget')
    require(identity(path.stat()) == before, 'Raw output changed during validation')
    return {'all_geometry_and_six_metadata_copies_match': True, 'raw_super': digest_file(path),
            'both_slot_payload_sha256': hashes, 'group_used_bytes': used,
            'nonoverlapping_extents': ranges, 'source_images_unchanged_by_builder': True}


def publish_result(output_dir, result):
    """Expose only a complete report; a failed write never publishes success."""
    temporary = output_dir/'result.json.tmp'
    try:
        with temporary.open('x') as report:
            report.write(json.dumps(result, indent=2)+'\n')
            report.flush()
            os.fsync(report.fileno())
        os.replace(temporary, output_dir/'result.json')
    finally:
        temporary.unlink(missing_ok=True)


def build(plan, output_dir):
    require(plan['status'] == 'verified-input-plan-only', 'Invalid plan')
    fresh = make_plan(plan['audit']['path'], plan['audit']['sha256'],
                      plan['lpmake']['path'], plan['lpmake']['sha256'], plan['layout'])
    require(fresh == plan, 'Stale/modified plan')
    guarded = [plan['audit'], plan['lpmake'], *plan['images'].values()]
    before_inputs = {row['path']: identity(local(row['path']).stat()) for row in guarded}
    output_dir = local(output_dir, output=True)
    require(not output_dir.exists(), 'Refusing existing output directory')
    require(shutil.disk_usage(output_dir.parent).free >= plan['layout']['super_bytes'],
            'Insufficient worst-case raw-output storage')
    output_dir.mkdir()
    dest = output_dir/'super-both.raw.img'
    argv = command(plan, dest)
    result = {'status': 'incomplete', 'command': argv, 'plan': plan, 'hardware_accessed': False}
    try:
        with (output_dir/'lpmake.log').open('wb') as log:
            run = subprocess.run(argv, cwd=output_dir, stdout=log, stderr=subprocess.STDOUT)
        require(run.returncode == 0, 'lpmake failed: '+str(run.returncode))
        checked_record(plan['audit']); checked_record(plan['lpmake'])
        for row in plan['images'].values(): checked_record(row)
        result['validation'] = validate_raw(dest, plan)
        require(all(identity(local(path).stat()) == before for path, before in before_inputs.items()),
                'Pinned input changed during build/validation')
        result['status'] = 'verified-both-slots-populated-raw-super'
        publish_result(output_dir, result)
        return result
    except Exception as e:
        result['status'] = 'incomplete'
        result['error'] = str(e)
        # Cleanup must not depend on being able to create the error report: the
        # filesystem may be full, which can itself cause publication to fail.
        try:
            if dest.is_file() and not dest.is_symlink(): dest.unlink()
        except OSError as cleanup_error:
            result['cleanup_error'] = str(cleanup_error)
            e.add_note('Raw output cleanup failed: ' + str(cleanup_error))
        try:
            publish_result(output_dir, result)
        except Exception as report_error:
            e.add_note('Incomplete result report failed: ' + str(report_error))
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('plan')
    for flag in ['audit', 'audit-sha256', 'lpmake', 'lpmake-sha256', 'output']:
        p.add_argument('--'+flag, required=True)
    p = sub.add_parser('build')
    for flag in ['plan', 'plan-sha256', 'output-dir']: p.add_argument('--'+flag, required=True)
    a = ap.parse_args()
    try:
        if a.mode == 'plan':
            output = local(a.output, output=True)
            require(not output.exists(), 'Refusing existing plan output')
            plan = make_plan(a.audit, a.audit_sha256, a.lpmake, a.lpmake_sha256)
            with output.open('x') as f: json.dump(plan, f, indent=2); f.write('\n')
        else:
            pin = digest_file(a.plan); require(pin['sha256'] == a.plan_sha256, 'Plan SHA256 mismatch')
            data = Path(pin['path']).read_bytes()
            require(hashlib.sha256(data).hexdigest() == a.plan_sha256, 'Plan changed before parsing')
            plan = json.loads(data)
            require(plan['layout'] == LAYOUT, 'CLI accepts only actual r1 dimensions')
            build(plan, a.output_dir)
    except (ValueError, OSError, KeyError, TypeError, struct.error) as e:
        ap.exit(1, 'Rejected: '+str(e)+'\n')

if __name__ == '__main__': main()
