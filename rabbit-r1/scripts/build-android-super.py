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
LEGACY_CHECKS = (
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


SOC_CHECKS = ('completed build and staged provenance',
 'image hashes and logical partition budgets',
 'actual check-all-partition-sizes result',
 'full vbmeta signature, descriptors and image correspondence',
 'sparse super metadata, A/B layout and actual extent payloads',
 'actual ext4 contents: init, fstab, VINTF, services, app and modules',
 'packaged ARM64 services and charging APK metadata',
 'all SOC kernel modules and explicit pstore-load packaging',
 'SOC cp2a product flags and embedded kernel configuration',
 'actual SOC Health linked binaries and init/VINTF separation',
 'compiled SOC split and recovery policy',
 'SOC Health service, helper, init/VINTF and policy inside vendor image',
 'packaged finalized Android 17 and Lineage 24 property values',
 'current SOC boot/DTBO headers, AVB coverage and recovery contents',
 'current packaged pstore collector and liblog implementations',
 'resolved ARM allocator configuration',
 'current allocator APEX payload, ARM64 libraries, init and VINTF',
 'image immutability during read-only audit')
AUDIT_PROFILES = {'soc-cp2a-18': SOC_CHECKS, 'legacy-default-10': LEGACY_CHECKS}
SOC_KERNEL = 'e98ffe5fcee6ce89fb754003059334ba1cf50b5c'
SOC_PRODUCER = {'verify.py': '04f03038f1b4c7ae60b30cf1cbb1f49edeb87c14676255233d746d2d6a10a28e',
 'provenance.py': '36f06211b062b409ae42833c54e3e51e2c3e493861305d040101028644b00473',
 'health_checks.py': '5562be046530280189632a49b3b9e23b1a10a175ad1df17e4f50d04a402a6176'}
SOC_TARGETS = {'bootimage',
 'check-all-partition-sizes',
 'dtboimage',
 'productimage',
 'sepolicy.recovery',
 'superimage',
 'systemextimage',
 'systemimage',
 'vbmetaimage',
 'vendorimage'}
SOC_DETAIL_KEYS = {'completed build and staged provenance': {'build_identity',
                                           'build_log',
                                           'build_start_commit',
                                           'canonical_templates_verified',
                                           'installer_manifest',
                                           'kernel_source_commit',
                                           'staged_files_verified'},
 'image hashes and logical partition budgets': {'boot',
                                                'dtbo',
                                                'dynamic_group',
                                                'product',
                                                'super',
                                                'system',
                                                'system_ext',
                                                'vbmeta',
                                                'vendor'},
 'actual check-all-partition-sizes result': {'group_budget_independently_checked',
                                             'input',
                                             'log',
                                             'warnings'},
 'full vbmeta signature, descriptors and image correspondence': {'boot',
                                                                 'descriptor_payloads_verified',
                                                                 'dtbo',
                                                                 'fec_parity_verified',
                                                                 'hashtrees_verified',
                                                                 'product',
                                                                 'system',
                                                                 'system_ext',
                                                                 'trusted_production_chain',
                                                                 'vbmeta',
                                                                 'vendor'},
 'sparse super metadata, A/B layout and actual extent payloads': {'empty_slot',
                                                                  'geometry',
                                                                  'logical_extent_image_sha256',
                                                                  'metadata',
                                                                  'nonoverlapping_extents',
                                                                  'populated_slot',
                                                                  'primary_backup_and_all_slots_identical'},
 'actual ext4 contents: init, fstab, VINTF, services, app and modules': {'product',
                                                                         'system',
                                                                         'system_ext',
                                                                         'vendor'},
 'packaged ARM64 services and charging APK metadata': {'native_elfs', 'charging_apk', 'package'},
 'all SOC kernel modules and explicit pstore-load packaging': {'kernel_release',
                                                               'module_dependencies_sha256',
                                                               'module_files'},
 'SOC cp2a product flags and embedded kernel configuration': {'generated_variables',
                                                              'health_mode',
                                                              'kernel_record',
                                                              'resolved_compiler_flag',
                                                              'values'},
 'actual SOC Health linked binaries and init/VINTF separation': {'binaries', 'init_vintf'},
 'compiled SOC split and recovery policy': {'compiled',
                                            'compiled_normal_label_and_access_checks',
                                            'health_context_lookup',
                                            'literal_genfs_prefix_capability_checked',
                                            'split_inputs',
                                            'vendor_cil'},
 'SOC Health service, helper, init/VINTF and policy inside vendor image': {'actual_vendor_image_members',
                                                                           'default_vendor_health_absent',
                                                                           'normal_health_inventory',
                                                                           'split_policy_packaged_bytes_match_current_compiled_outputs'},
 'packaged finalized Android 17 and Lineage 24 property values': {'declared_security_patch_is_not_coverage_proof',
                                                                  'values'},
 'current SOC boot/DTBO headers, AVB coverage and recovery contents': {'boot',
                                                                       'dt_table',
                                                                       'dtbo',
                                                                       'header',
                                                                       'recovery_dependency_closure',
                                                                       'recovery_files',
                                                                       'unique_health_fragments',
                                                                       'unique_health_services'},
 'current packaged pstore collector and liblog implementations': {'actual_packaged_artifacts',
                                                                  'old_build_hashes_reused'},
 'resolved ARM allocator configuration': {'minigbm_config', 'variables'},
 'current allocator APEX payload, ARM64 libraries, init and VINTF': {'bytes',
                                                                     'elfs',
                                                                     'manifest',
                                                                     'path',
                                                                     'payload',
                                                                     'required_files',
                                                                     'sha256',
                                                                     'vintf_declarations'},
 'image immutability during read-only audit': {'image_sizes_and_mtimes_unchanged',
                                               'no_writable_image_handles_used',
                                               'source_records_and_full_artifact_hashes'}}
LEGACY_WARNING = ('Historical default-product reproduction only: this ten-check audit predates '
                  'the corrected Health provider inventory and did not reject the old Cuttlefish mock Health HAL. '
                  'It is not current SOC complete-image or clean-install evidence.')


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


SOC_BOOLEAN_DETAILS = {
    'group_budget_independently_checked': True, 'descriptor_payloads_verified': True,
    'hashtrees_verified': True, 'fec_parity_verified': False, 'trusted_production_chain': False,
    'primary_backup_and_all_slots_identical': True, 'compiled_normal_label_and_access_checks': True,
    'literal_genfs_prefix_capability_checked': True, 'default_vendor_health_absent': True,
    'split_policy_packaged_bytes_match_current_compiled_outputs': True,
    'declared_security_patch_is_not_coverage_proof': True, 'old_build_hashes_reused': False,
    'image_sizes_and_mtimes_unchanged': True, 'no_writable_image_handles_used': True,
}
SOC_STRING_DETAILS = {'build_start_commit', 'kernel_source_commit', 'package', 'kernel_release',
                      'module_dependencies_sha256', 'resolved_compiler_flag', 'populated_slot',
                      'empty_slot', 'path', 'sha256'}
SOC_INTEGER_DETAILS = {'staged_files_verified', 'canonical_templates_verified', 'bytes'}
SOC_LIST_DETAILS = {'warnings', 'nonoverlapping_extents', 'unique_health_services', 'unique_health_fragments'}


def record_shape(row):
    require(isinstance(row, dict) and {'path', 'bytes', 'sha256'} <= row.keys(),
            'Incomplete artifact record')
    require(isinstance(row['path'], str) and Path(row['path']).is_absolute()
            and type(row['bytes']) is int and row['bytes'] > 0
            and isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']),
            'Malformed artifact record')
    local(row['path'])  # No file content is opened by this shape check.
    return {k: row[k] for k in ('path', 'bytes', 'sha256')}


def read_pinned_json(row, dependencies):
    pin = checked_record(record_shape(row)); dependencies.append(pin)
    data = Path(pin['path']).read_bytes()
    require(hashlib.sha256(data).hexdigest() == pin['sha256'], 'JSON changed before parsing')
    obj = json.loads(data)
    require(isinstance(obj, dict), 'Expected pinned JSON object')
    return obj


def soc_audit(audit, checks, dependencies):
    """Consume the frozen SOC producer contract; never substitute audit execution."""
    # Pin imported producer code too: a same-named set of pass rows alone is
    # not the reviewed producer. Source changes require an explicit new profile.
    verifier = record_shape(audit['verifier'])
    parent = Path(verifier['path']).parent
    for name, wanted in SOC_PRODUCER.items():
        pin = digest_file(parent/name)
        require(pin['sha256'] == wanted, 'Unreviewed SOC audit producer: ' + name)
        dependencies.append(pin)
    require(Path(verifier['path']).name == 'verify.py', 'SOC verifier name differs')
    for name, required in SOC_DETAIL_KEYS.items():
        details = checks[name]
        require(isinstance(details, dict) and required <= details.keys(),
                'Incomplete SOC check details: ' + name)
        for key in required:
            value = details[key]
            if key in SOC_BOOLEAN_DETAILS:
                require(value is SOC_BOOLEAN_DETAILS[key], 'SOC detail scope differs: ' + key)
            else:
                kind = (str if key in SOC_STRING_DETAILS else int if key in SOC_INTEGER_DETAILS
                        else list if key in SOC_LIST_DETAILS else dict)
                require(type(value) is kind and (key == 'warnings' or bool(value)),
                        'Malformed SOC detail: ' + name + ':' + key)
    provenance = checks[SOC_CHECKS[0]]
    source = audit['build_start_source_commit']
    identity = provenance['build_identity']
    require(isinstance(identity, dict)
            and identity['expected_source_commit'] == source
            and identity['expected_kernel_commit'] == SOC_KERNEL
            and identity['product'] == 'lineage_r1_soc' and identity['release'] == 'cp2a'
            and isinstance(identity['completed_session'], str)
            and re.fullmatch('[0-9]+', identity['completed_session']),
            'Incomplete SOC build identity')
    source_keys = {'build_start_commit', 'installer_manifest', 'staged_files_verified',
                   'canonical_templates_verified', 'templates', 'generated_inputs_pinned_by_build_start',
                   'kernel_source_commit', 'android_patch_series_sha256', 'sources_lock_sha256',
                   'verified_applied_patches'}
    require(isinstance(identity['source_identity'], dict)
            and set(identity['source_identity']) == source_keys
            and identity['source_identity'] == {k: provenance[k] for k in source_keys}
            and provenance['kernel_source_commit'] == SOC_KERNEL
            and type(provenance['staged_files_verified']) is int and provenance['staged_files_verified'] > 0
            and type(provenance['canonical_templates_verified']) is int and provenance['canonical_templates_verified'] > 0,
            'SOC source identity differs')
    require(record_shape(identity['guard']) == record_shape(next(
        row for row in dependencies if row['path'] == str(parent/'provenance.py'))),
        'SOC guard record differs')
    start = read_pinned_json(identity['build_start'], dependencies)
    result = read_pinned_json(identity['build_result'], dependencies)
    require(start['build_source_commit'] == source
            and (start['product'], start['release'], start['variant']) == ('lineage_r1_soc', 'cp2a', 'userdebug')
            and isinstance(start['targets'], list) and SOC_TARGETS <= set(start['targets']),
            'SOC build-start context differs')
    require(type(result['exit_code']) is int and result['exit_code'] == 0
            and result['inputs_unchanged'] is True and result.get('build_source_commit', source) == source
            and result['build_start'] == identity['build_start']['path']
            and result['build_start_sha256'] == identity['build_start']['sha256'],
            'SOC completed-result linkage differs')
    log = record_shape(provenance['build_log'])
    require(record_shape(identity['build_log']) == log and result['log'] == log['path']
            and result['log_sha256'] == log['sha256']
            and ('log' not in start or start['log'] == log['path']), 'SOC build-log linkage differs')
    require('Android configuration: lineage_r1_soc cp2a userdebug' in Path(log['path']).read_text(errors='replace'),
            'SOC wrapper configuration marker missing')
    installer = record_shape(provenance['installer_manifest'])
    require(start['inputs']['installer_sha256'] == installer['sha256'], 'SOC installer pin differs')
    inputs = audit['source_inputs']
    require(isinstance(inputs, list) and len(inputs) == 12
            and len({row['path'] for row in inputs}) == 12
            and installer in [record_shape(row) for row in inputs], 'Incomplete SOC source inputs')
    for row in inputs: dependencies.append(checked_record(record_shape(row)))
    closed = checks[SOC_CHECKS[-1]]
    require(closed['image_sizes_and_mtimes_unchanged'] is True
            and closed['no_writable_image_handles_used'] is True
            and closed['source_records_and_full_artifact_hashes']['unchanged'] is True
            and closed['source_records_and_full_artifact_hashes']['source_commit'] == source
            and type(closed['source_records_and_full_artifact_hashes']['watched_artifacts']) is int
            and closed['source_records_and_full_artifact_hashes']['watched_artifacts'] > 0,
            'SOC final source/artifact guard did not close')
    sizes = checks[SOC_CHECKS[1]]
    names = (*PARTS, 'super', 'vbmeta', 'boot', 'dtbo')
    require(set(sizes) == set(names) | {'dynamic_group'}, 'Incomplete SOC image set')
    records = {name: record_shape(sizes[name]) for name in names}
    image_parent = Path(records['system']['path']).parent
    require(all(Path(row['path']) == image_parent/(name+'.img') for name,row in records.items())
            and len(audit['expected_images']) == 8
            and set(audit['expected_images']) == {row['path'] for row in records.values()},
            'SOC image paths differ')
    require(sizes['super']['android_sparse'] is True and sizes['super']['logical_bytes'] == LAYOUT['super_bytes']
            and sizes['boot']['logical_bytes'] == sizes['boot']['bytes'] == 33554432
            and sizes['dtbo']['logical_bytes'] == sizes['dtbo']['bytes'] == 8388608,
            'SOC fixed image dimensions differ')
    total = sum(sizes[n]['bytes'] for n in PARTS)
    require(sizes['dynamic_group'] == {'image_total_bytes': total, 'maximum_bytes': LAYOUT['group_bytes'],
                                      'remaining_bytes': LAYOUT['group_bytes']-total}, 'SOC group summary differs')
    super_details = checks[SOC_CHECKS[4]]
    require(super_details['populated_slot'] == 'a' and super_details['empty_slot'] == 'b'
            and super_details['primary_backup_and_all_slots_identical'] is True
            and super_details['logical_extent_image_sha256'] == {n+'_a': records[n]['sha256'] for n in PARTS},
            'SOC factory-super payload correspondence differs')
    avb = checks[SOC_CHECKS[3]]
    require(avb['descriptor_payloads_verified'] is True and avb['hashtrees_verified'] is True
            and avb['fec_parity_verified'] is False and avb['trusted_production_chain'] is False,
            'SOC AVB scope differs')
    boot = checks[SOC_CHECKS[13]]
    require(record_shape(boot['boot']) == records['boot'] and record_shape(boot['dtbo']) == records['dtbo'],
            'SOC audited boot/DTBO correspondence differs')
    flags = checks[SOC_CHECKS[8]]
    mode = flags['health_mode']
    require(mode['product'] == 'lineage_r1_soc' and mode['experimental_soc_health'] is True
            and mode['soc_config'] == 'y' and mode['embedded_config_verified'] is True
            and flags['resolved_compiler_flag'] == 'clang-r596125'
            and flags['values']['DeviceProduct'] == 'lineage_r1_soc'
            and flags['values']['Platform_version_name'] == '17'
            and flags['values']['Platform_sdk_version'] == 37
            and flags['values']['VendorApiLevel'] == '202604', 'SOC Health/kernel product context differs')
    require(record_shape(flags['kernel_record']) in [record_shape(row) for row in inputs],
            'SOC kernel record not included in pinned source inputs')
    linked = checks[SOC_CHECKS[9]]
    require(set(linked['binaries']) == {'vendor-service', 'initial-readiness', 'recovery-service'}
            and linked['init_vintf']['normal_boot_readiness_only'] is True, 'SOC Health/recovery separation differs')
    policy = checks[SOC_CHECKS[10]]
    require(set(policy['compiled']) == {'normal', 'recovery'}
            and policy['compiled_normal_label_and_access_checks'] is True
            and policy['literal_genfs_prefix_capability_checked'] is True,
            'SOC policy detail missing')
    packaged = checks[SOC_CHECKS[11]]
    require(packaged['default_vendor_health_absent'] is True
            and packaged['split_policy_packaged_bytes_match_current_compiled_outputs'] is True,
            'SOC packaged Health check incomplete')
    inventory = packaged['normal_health_inventory']
    require(inventory['cuttlefish_health_apex_absent'] is True
            and inventory['normal_health_vintf_providers'] == [{'partition':'vendor',
                'path':'/etc/vintf/manifest/health.r1.xml','format':'aidl','version':'5','fqnames':['IHealth/default']}],
            'SOC competing/default Health provider present')
    expected_services = [{'partition':'vendor','path':'/etc/init/health.r1.rc','name':name,'command':argv}
        for name,argv in [('vendor.health-default',['/vendor/bin/hw/android.hardware.health-service.r1']),
                          ('r1-health-initial-ready',['/vendor/bin/r1-health-initial-ready']),
                          ('vendor.charger',['/vendor/bin/hw/android.hardware.health-service.r1','--charger'])]]
    require(sorted(inventory['normal_health_init_services'], key=lambda x:x['name']) ==
            sorted(expected_services, key=lambda x:x['name']), 'SOC Health service inventory differs')
    for path,key in [('/bin/hw/android.hardware.health-service.r1','vendor-service'),
                     ('/bin/r1-health-initial-ready','initial-readiness')]:
        packaged_row = record_shape(packaged['actual_vendor_image_members'][path])
        filesystem_row = record_shape(checks[SOC_CHECKS[5]]['vendor']['selected_files'][path])
        linked_row = record_shape(linked['binaries'][key])
        require(packaged_row == filesystem_row
                and all(packaged_row[k] == linked_row[k] for k in ('bytes','sha256')),
                'SOC packaged/linked Health bytes differ')
    require(checks[SOC_CHECKS[15]]['minigbm_config']['platform'] == 'all_arm', 'SOC allocator architecture differs')
    # The other image hashes also belong to the completed audit, even though
    # only the four filesystem images become lpmake payloads. Validate them
    # only after this complete schema/provenance gate, in make_plan().
    return [records[n] for n in ('super', 'vbmeta', 'boot', 'dtbo')]

def read_audit(path, expected, audit_profile):
    # All completion gates run before any logical filesystem image is opened.
    pin = digest_file(path)
    require(pin['sha256'] == expected, 'Audit SHA256 mismatch')
    data = Path(pin['path']).read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, 'Audit changed before parsing')
    audit = json.loads(data)
    require(isinstance(audit, dict), 'Expected audit object')
    require(audit.get('mode') == 'verify' and audit.get('overall') == 'pass'
            and audit.get('errors') == [], 'A completed passing image audit is required')
    require(audit_profile in AUDIT_PROFILES, 'Unknown audit profile')
    required = AUDIT_PROFILES[audit_profile]
    rows = audit.get('checks', [])
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
            and len(rows) == len(required) and {r.get('name') for r in rows} == set(required)
            and all(r.get('status') == 'pass' and isinstance(r.get('details'), dict) for r in rows), 'Incomplete/failed audit checks')
    checks = {r['name']: r['details'] for r in rows}
    provenance = checks[required[0]]
    commit = audit.get('build_start_source_commit', '')
    require(re.fullmatch('[0-9a-f]{40}', commit)
            and provenance.get('build_start_commit') == commit, 'Build revision mismatch')
    dependencies = [checked_record(audit['verifier'])]
    log = checked_record(provenance['build_log']); dependencies.append(log)
    require('#### build completed successfully' in Path(log['path']).read_text(errors='replace'),
            'Successful pinned build log missing')
    require(checks[required[-1]].get('image_sizes_and_mtimes_unchanged') is True,
            'Audit did not finish image immutability check')
    images = soc_audit(audit, checks, dependencies) if audit_profile == 'soc-cp2a-18' else []
    return pin, audit, checks, dependencies, images


def make_plan(audit_path, audit_sha256, tool_path, tool_sha256, layout=None,
              audit_profile='soc-cp2a-18'):
    # Alternate dimensions exist only for imported synthetic tests; the CLI fixes r1.
    layout = dict(LAYOUT if layout is None else layout)
    pin, audit, checks, dependencies, other_images = read_audit(audit_path, audit_sha256, audit_profile)
    tool = digest_file(tool_path)
    require(tool['sha256'] == tool_sha256 and os.access(tool['path'], os.X_OK),
            'Pinned executable lpmake missing/changed')
    sizes = checks[AUDIT_PROFILES[audit_profile][1]]
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
    for row in other_images: dependencies.append(checked_record(row))
    unique = {}
    for row in dependencies:
        require(row['path'] not in unique or unique[row['path']] == row, 'Conflicting audit dependency pins')
        unique[row['path']] = row
    return {'schema': 2, 'status': 'verified-input-plan-only', 'audit': pin,
            'audit_profile': audit_profile, 'audit_dependencies': list(unique.values()),
            'historical_reproduction_only': audit_profile == 'legacy-default-10',
            'audit_limitations': [LEGACY_WARNING] if audit_profile == 'legacy-default-10' else [],
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


def sync_file(path):
    """Surface delayed write errors before validating/publishing an output."""
    path = local(path)
    require(stat.S_ISREG(path.lstat().st_mode), 'Not a regular output: ' + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), 'Not a regular output: ' + str(path))
        os.fsync(fd)
        require(identity(before) == identity(os.fstat(fd)) == identity(path.stat()),
                'Output changed during fsync: ' + str(path))
        return identity(before)
    finally:
        os.close(fd)


def sync_directory(path):
    path = local(path, output=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_result(output_dir, result):
    """Sync a complete report and its directory; roll back sync failure."""
    temporary = output_dir/'result.json.tmp'
    final = output_dir/'result.json'
    published = False
    try:
        with temporary.open('x') as report:
            report.write(json.dumps(result, indent=2)+'\n')
            report.flush()
            os.fsync(report.fileno())
        os.replace(temporary, final)
        published = True
        sync_directory(output_dir)
    except Exception as e:
        # A failed post-rename directory sync must not leave a live success
        # report while build() removes the raw output. Rollback durability can
        # itself fail; preserve that error rather than claim a durable rollback.
        if published:
            try:
                final.unlink(missing_ok=True)
                sync_directory(output_dir)
            except OSError as cleanup_error:
                e.add_note('Published result cleanup failed: ' + str(cleanup_error))
        raise
    finally:
        temporary.unlink(missing_ok=True)


def build(plan, output_dir):
    require(plan['schema'] == 2 and plan['status'] == 'verified-input-plan-only', 'Invalid/profile-less plan')
    fresh = make_plan(plan['audit']['path'], plan['audit']['sha256'],
                      plan['lpmake']['path'], plan['lpmake']['sha256'], plan['layout'], plan['audit_profile'])
    require(fresh == plan, 'Stale/modified plan')
    guarded = [plan['audit'], plan['lpmake'], *plan['images'].values(), *plan['audit_dependencies']]
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
        sync_directory(output_dir.parent)
        with (output_dir/'lpmake.log').open('wb') as log:
            run = subprocess.run(argv, cwd=output_dir, stdout=log, stderr=subprocess.STDOUT)
        require(run.returncode == 0, 'lpmake failed: '+str(run.returncode))
        synced_output = sync_file(dest)
        sync_file(output_dir/'lpmake.log')
        checked_record(plan['audit']); checked_record(plan['lpmake'])
        for row in [*plan['images'].values(), *plan['audit_dependencies']]: checked_record(row)
        result['validation'] = validate_raw(dest, plan)
        sync_directory(output_dir)
        require(identity(local(dest).stat()) == synced_output,
                'Raw output changed after fsync/validation')
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
    p.add_argument('--audit-profile', choices=sorted(AUDIT_PROFILES), default='soc-cp2a-18',
                   help='Current SOC audit by default; legacy-default-10 is historical reproduction only')
    for flag in ['audit', 'audit-sha256', 'lpmake', 'lpmake-sha256', 'output']:
        p.add_argument('--'+flag, required=True)
    p = sub.add_parser('build')
    for flag in ['plan', 'plan-sha256', 'output-dir']: p.add_argument('--'+flag, required=True)
    a = ap.parse_args()
    try:
        if a.mode == 'plan':
            output = local(a.output, output=True)
            require(not output.exists(), 'Refusing existing plan output')
            plan = make_plan(a.audit, a.audit_sha256, a.lpmake, a.lpmake_sha256, audit_profile=a.audit_profile)
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
