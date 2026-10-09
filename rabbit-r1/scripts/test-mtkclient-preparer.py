#!/usr/bin/env python3
"""Actual pinned-archive preparation and rejection controls; no target imports."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent
PROJECT = SCRIPTS.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', type=Path, required=True, help='new private result directory under /rabbitr1')
parser.add_argument('--script', type=Path, default=SCRIPTS / 'prepare-mtkclient.py')
parser.add_argument('--manifest', type=Path, default=PROJECT / 'mtkclient/transport.json')
parser.add_argument('--patch', type=Path, default=PROJECT / 'patches/mtkclient/0001-guard-bulk-transfers.patch')
args = parser.parse_args()
ARCHIVE = Path('/rabbitr1/downloads/mtkclient-v2.1.4.1.tar.gz')
SCRIPT, MANIFEST, PATCH = args.script, args.manifest, args.patch
spec = importlib.util.spec_from_file_location('preparer', SCRIPT)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
OUT = p.local_path(args.out)
OUT.mkdir()  # Existing output is rejected; the caller provides its parent.
CONTROLS = []
COMMANDS = []
WORK = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def control(name):
    CONTROLS.append(name)


def run(destination, check=False, manifest=MANIFEST, patch=PATCH, archive=ARCHIVE):
    command = [sys.executable, str(SCRIPT), '--manifest', str(manifest),
               '--patch', str(patch), '--archive', str(archive), '--destination', str(destination)]
    if check:
        command.append('--check')
    result = subprocess.run(command, capture_output=True, text=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    COMMANDS.append({'argv': command, 'returncode': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    return result


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = WORK / 'tree'
        cls.inputs = p.read_inputs(MANIFEST, ARCHIVE, PATCH)
        result = run(cls.tree)
        if result.returncode:
            raise AssertionError(result.stderr)
        control('actual archive install: 1164 files, 40 directories')

    def verify(self):
        _, _, files, dirs, changes = self.inputs
        p.verify_tree(self.tree, files, dirs, changes)

    def rejected_check(self, label):
        with self.assertRaises((ValueError, OSError)):
            self.verify()
        control(label)

    def test_01_positive_and_existing(self):
        result = run(self.tree, check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['files'], 1164)
        control('actual CLI check without importing target modules')
        self.assertNotEqual(run(self.tree).returncode, 0)
        self.verify()
        control('existing valid destination is never overwritten')
        empty = WORK / 'empty-existing'
        empty.mkdir()
        inode = empty.stat().st_ino
        self.assertNotEqual(run(empty).returncode, 0)
        self.assertEqual(empty.stat().st_ino, inode)
        self.assertEqual(list(empty.iterdir()), [])
        control('existing empty destination is never overwritten')

    def test_02_changes_and_missing(self):
        for relative in ['README.md', 'mtkclient/Library/Port.py']:
            path = self.tree / relative
            saved = path.read_bytes()
            try:
                path.write_bytes(saved + b'\n# local change\n')
                self.rejected_check('modified file rejected: ' + relative)
            finally:
                path.write_bytes(saved)
        path = self.tree / 'Tools/parsebootrom.txt'
        self.assertEqual(path.stat().st_size, 0)
        mode = path.stat().st_mode & 0o777
        path.unlink()
        try:
            self.rejected_check('missing zero-byte upstream file rejected')
        finally:
            path.touch(mode=mode)
            path.chmod(mode)
        self.verify()

    def test_03_links_extras_modes(self):
        path = self.tree / 'README.md'
        saved = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        path.unlink()
        path.symlink_to(WORK / 'nonexistent')
        try:
            self.rejected_check('dangling file symlink rejected')
        finally:
            path.unlink()
            path.write_bytes(saved)
            path.chmod(mode)
        link = self.tree / 'extra-dir-link'
        link.symlink_to(WORK, target_is_directory=True)
        try:
            self.rejected_check('directory symlink rejected without traversal')
        finally:
            link.unlink()
        link = WORK / 'hardlink'
        os.link(path, link)
        try:
            self.rejected_check('aliased hardlink rejected')
        finally:
            link.unlink()
        for relative, directory in [('__pycache__', True), ('local-extra.py', False)]:
            extra = self.tree / relative
            extra.mkdir() if directory else extra.write_bytes(b'extra')
            try:
                self.rejected_check('unexpected entry rejected: ' + relative)
            finally:
                extra.rmdir() if directory else extra.unlink()
        path.chmod(mode ^ 0o010)
        try:
            self.rejected_check('file permission change rejected')
        finally:
            path.chmod(mode)
        self.verify()

    def test_04_bad_pins(self):
        manifest = json.loads(MANIFEST.read_text())
        for key in ['archive', 'patch']:
            changed = json.loads(json.dumps(manifest))
            changed[key]['sha256'] = '0' * 64
            bad = WORK / ('bad-' + key + '.json')
            bad.write_text(json.dumps(changed))
            destination = WORK / ('bad-' + key)
            self.assertNotEqual(run(destination, manifest=bad).returncode, 0)
            self.assertFalse(destination.exists())
            control(key + ' pin mismatch rejected before publication')
        changed = json.loads(json.dumps(manifest))
        changed['files'][0]['after_sha256'] = '0' * 64
        bad = WORK / 'bad-after.json'
        bad.write_text(json.dumps(changed))
        destination = WORK / 'bad-after'
        self.assertNotEqual(run(destination, manifest=bad).returncode, 0)
        self.assertFalse(destination.exists())
        self.assertFalse(list(WORK.glob('.mtkclient-prepare-*')))
        control('wrong after pin rejects staged tree and removes only own staging')
        for count in (5, 7):
            changed = json.loads(json.dumps(manifest))
            changed['files'] = (changed['files'] + changed['files'])[:count]
            bad = WORK / ('bad-count-' + str(count) + '.json')
            bad.write_text(json.dumps(changed))
            destination = WORK / ('bad-count-' + str(count))
            self.assertNotEqual(run(destination, manifest=bad).returncode, 0)
            self.assertFalse(destination.exists())
            control('wrong patched-file count rejected: ' + str(count))

    def test_05_paths(self):
        linked = WORK / 'linked-archive'
        linked.symlink_to(ARCHIVE)
        destination = WORK / 'linked-source'
        self.assertNotEqual(run(destination, archive=linked).returncode, 0)
        self.assertFalse(destination.exists())
        control('symlink input rejected')
        linked_parent = WORK / 'linked-parent'
        linked_parent.symlink_to(WORK, target_is_directory=True)
        self.assertNotEqual(run(linked_parent / 'target').returncode, 0)
        self.assertFalse((WORK / 'target').exists())
        control('destination symlink ancestor rejected')
        result = run(Path('/rabbitr1/src/mtkclient'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('original mtkclient tree is protected', result.stderr)
        control('original source destination rejected before archive extraction')

    def test_06_publication_race(self):
        _, patch, files, directories, changes = self.inputs
        destination = WORK / 'publication-race'
        original_publish = p.publish_no_replace
        def occupied(source, target):
            target.mkdir()
            original_publish(source, target)
        p.publish_no_replace = occupied
        try:
            with self.assertRaises(FileExistsError):
                p.prepare(destination, files, directories, changes, patch)
        finally:
            p.publish_no_replace = original_publish
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(list(WORK.glob('.mtkclient-prepare-*')))
        control('real renameat2 rejects destination created at publication boundary')

    def test_07_final(self):
        self.verify()
        self.assertFalse(any(name == 'usb' or name.startswith('usb.') or
                             name == 'mtkclient' or name.startswith('mtkclient.')
                             for name in sys.modules))
        control('final fixture remains byte/mode exact; no USB or target module imports')


if __name__ == '__main__':
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    pins = {str(path): sha(path) for path in [SCRIPT, MANIFEST, PATCH, ARCHIVE]}
    original_paths = [Path('/rabbitr1/src/mtkclient') / entry['path']
                      for entry in json.loads(MANIFEST.read_text())['files']]
    originals = {str(path): sha(path) for path in original_paths}
    work = tempfile.TemporaryDirectory(prefix='test-fixture-', dir=OUT)
    WORK = Path(work.name)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    after = {str(path): sha(path) for path in map(Path, pins)}
    original_after = {str(path): sha(path) for path in original_paths}
    unchanged = pins == after and originals == original_after
    work.cleanup()
    record = {'status': 'PASS' if result.wasSuccessful() and unchanged else 'FAIL',
              'methods': result.testsRun, 'controls': CONTROLS, 'commands': COMMANDS,
              'inputs': pins, 'inputs_unchanged': pins == after,
              'original_sources': originals, 'original_sources_unchanged': originals == original_after,
              'fixture_removed': not WORK.exists(), 'scope': 'offline actual archive, no target imports/device I/O'}
    (OUT / 'test-result.json').write_text(json.dumps(record, indent=2) + '\n')
    raise SystemExit(0 if record['status'] == 'PASS' else 1)
