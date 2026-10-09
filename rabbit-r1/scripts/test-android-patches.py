#!/usr/bin/env python3
"""Exercise the Android patch CLI against isolated real Git repositories."""
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path('/rabbitr1')
HELPER = Path(__file__).resolve().with_name('apply-android-patches.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PatchTests(unittest.TestCase):
    def setUp(self):
        temporary = ROOT / '.tmp'
        if temporary.is_symlink():
            self.fail('Temporary directory must not be a symlink')
        temporary.mkdir(exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(prefix='android-patches-test-', dir=temporary)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.tree = self.root / 'tree'
        self.series_dir = self.root / 'patches'
        self.series_dir.mkdir()
        self.series = self.series_dir / 'series.json'
        self.records = []
        self.before = {}
        self.after = {}
        self.env = os.environ.copy()
        self.env.update(GIT_CONFIG_GLOBAL=str(ROOT / '.gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_DATE='2026-01-01T00:00:00Z',
                        GIT_COMMITTER_DATE='2026-01-01T00:00:00Z',
                        PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(temporary))
        for number in range(2):
            project = f'external/project{number}'
            checkout = self.tree / project
            checkout.mkdir(parents=True)
            self.git(checkout, 'init', '-q')
            self.git(checkout, 'config', 'user.name', 'itskenny0')
            self.git(checkout, 'config', 'user.email', '0@kenny.cat')
            files = []
            patch = 'From: itskenny0 <0@kenny.cat>\nSubject: [PATCH] Enable fixture\n\n---\n'
            for name in ('Android.bp', 'settings.txt'):
                before = f'project: {number}\nfile: {name}\nenabled: false\n'.encode()
                after = before.replace(b'enabled: false', b'enabled: true')
                (checkout / name).write_bytes(before)
                self.before[project, name] = before
                self.after[project, name] = after
                files.append({'path': name, 'before_sha256': digest(before),
                              'after_sha256': digest(after)})
                patch += ''.join(difflib.unified_diff(
                    before.decode().splitlines(True), after.decode().splitlines(True),
                    fromfile='a/' + name, tofile='b/' + name))
            (checkout / 'unrelated.txt').write_text('original unrelated file\n')
            self.git(checkout, 'add', '.')
            self.git(checkout, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Initial files')
            patch_name = f'{number}.patch'
            (self.series_dir / patch_name).write_text(patch)
            self.records.append({'project': project,
                                 'base_commit': self.git(checkout, 'rev-parse', 'HEAD').decode().strip(),
                                 'patch': patch_name, 'sha256': digest(patch.encode()), 'files': files})
        self.save()

    def git(self, checkout, *args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], env=self.env,
                                       stderr=subprocess.STDOUT)

    def save(self):
        self.series.write_text(json.dumps(self.records))

    def run_helper(self, *args, success=True):
        result = subprocess.run([sys.executable, str(HELPER), '--tree', str(self.tree),
                                 '--series', str(self.series), *args], cwd=ROOT,
                                env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        return result

    def touched(self, number=0, name='Android.bp'):
        return self.tree / self.records[number]['project'] / name

    def assert_original(self):
        for (project, name), before in self.before.items():
            self.assertEqual((self.tree / project / name).read_bytes(), before)

    def change_patch(self, number, transform):
        record = self.records[number]
        path = self.series_dir / record['patch']
        path.write_bytes(transform(path.read_bytes()))
        record['sha256'] = digest(path.read_bytes())
        self.save()

    def test_default_and_explicit_check_do_not_apply(self):
        for flags in ((), ('--check',)):
            result = self.run_helper(*flags)
            self.assertEqual(result.stdout.count('READY:'), 2)
            self.assert_original()
            for record in self.records:
                self.assertEqual(self.git(self.tree / record['project'], 'status', '--porcelain'), b'')

    def test_apply_and_idempotent_check_and_apply(self):
        result = self.run_helper('--apply')
        self.assertEqual(result.stdout.count('APPLIED:'), 2)
        for (project, name), content in self.after.items():
            self.assertEqual((self.tree / project / name).read_bytes(), content)
        for flags in (('--check',), ('--apply',)):
            result = self.run_helper(*flags)
            self.assertEqual(result.stdout.count('ALREADY-APPLIED:'), 2)
        for record in self.records:
            checkout = self.tree / record['project']
            self.assertEqual(self.git(checkout, 'diff', '--cached'), b'')
            self.assertEqual(self.git(checkout, 'rev-parse', 'HEAD').decode().strip(), record['base_commit'])

    def test_wrong_second_base_leaves_first_untouched(self):
        self.records[1]['base_commit'] = '0' * 40
        self.save()
        self.assertIn('Base HEAD mismatch', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_wrong_second_patch_hash_leaves_first_untouched(self):
        self.records[1]['sha256'] = '0' * 64
        self.save()
        self.assertIn('Patch hash mismatch', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_wrong_before_hash(self):
        self.records[1]['files'][0]['before_sha256'] = '0' * 64
        self.save()
        self.assertIn('Base file hash mismatch', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_wrong_after_hash_is_preflight_failure(self):
        self.records[1]['files'][0]['after_sha256'] = '0' * 64
        self.save()
        self.assertIn('Patch result hash mismatch', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_edited_touched_file_is_preserved(self):
        path = self.touched(1)
        path.write_text('local edit\n')
        self.assertIn('Preserving edited touched file', self.run_helper('--apply', success=False).stderr)
        self.assertEqual(path.read_text(), 'local edit\n')
        self.assertEqual(self.touched().read_bytes(), self.before['external/project0', 'Android.bp'])

    def test_staged_touched_file_is_preserved(self):
        project = self.records[1]['project']
        path = self.touched(1)
        path.write_bytes(self.after[project, 'Android.bp'])
        self.git(path.parent, 'add', 'Android.bp')
        staged = self.git(path.parent, 'diff', '--cached')
        self.assertIn('Preserving staged touched-file changes', self.run_helper('--apply', success=False).stderr)
        self.assertEqual(self.git(path.parent, 'diff', '--cached'), staged)
        self.assertEqual(path.read_bytes(), self.after[project, 'Android.bp'])
        self.assertEqual(self.touched().read_bytes(), self.before['external/project0', 'Android.bp'])

    def test_unrelated_staged_and_unstaged_edits_are_preserved(self):
        checkout = self.touched().parent
        path = checkout / 'unrelated.txt'
        path.write_text('staged change\n')
        self.git(checkout, 'add', 'unrelated.txt')
        path.write_text('further unstaged change\n')
        (checkout / 'untracked.txt').write_text('keep me\n')
        staged = self.git(checkout, 'diff', '--cached')
        self.run_helper('--apply')
        self.assertEqual(self.git(checkout, 'diff', '--cached'), staged)
        self.assertEqual(path.read_text(), 'further unstaged change\n')
        self.assertEqual((checkout / 'untracked.txt').read_text(), 'keep me\n')

    def test_partial_touched_state_is_rejected(self):
        self.touched(1).write_bytes(self.after['external/project1', 'Android.bp'])
        self.assertIn('Partially applied patch', self.run_helper('--apply', success=False).stderr)
        self.assertEqual(self.touched().read_bytes(), self.before['external/project0', 'Android.bp'])

    def test_invalid_patch_is_preflight_failure(self):
        self.change_patch(1, lambda patch: patch.replace(b'-enabled: false', b'-enabled: missing'))
        self.run_helper('--apply', success=False)
        self.assert_original()

    def test_reverse_check_rejects_inconsistent_patch(self):
        self.run_helper('--apply')
        self.change_patch(1, lambda patch: patch.replace(b'+enabled: true', b'+enabled: missing'))
        result = self.run_helper('--apply', success=False)
        self.assertIn('--reverse', result.stderr)
        for (project, name), content in self.after.items():
            self.assertEqual((self.tree / project / name).read_bytes(), content)

    def test_project_path_escape_is_rejected(self):
        self.records[1]['project'] = '../escape'
        self.save()
        self.run_helper('--apply', success=False)
        self.assert_original()

    def test_touched_path_escape_is_rejected(self):
        self.records[1]['files'][0]['path'] = '../escape'
        self.save()
        self.run_helper('--apply', success=False)
        self.assert_original()

    def test_patch_filename_escape_is_rejected(self):
        self.records[1]['patch'] = '../escape.patch'
        self.save()
        self.run_helper('--apply', success=False)
        self.assert_original()

    def test_patch_cannot_touch_undeclared_file(self):
        self.change_patch(1, lambda patch: patch.replace(b'/settings.txt', b'/unrelated.txt'))
        self.assertIn('Patch paths do not match', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_patch_content_path_escape_is_rejected(self):
        sentinel = self.tree / 'external' / 'escape'
        sentinel.write_text('unchanged\n')
        self.change_patch(1, lambda patch: patch.replace(b'/settings.txt', b'/../escape'))
        self.run_helper('--apply', success=False)
        self.assertEqual(sentinel.read_text(), 'unchanged\n')
        self.assert_original()

    def test_touched_symlink_is_rejected(self):
        path = self.touched(1)
        original = path.read_bytes()
        target = self.root / 'symlink-target'
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
        self.assertIn('Symlink path', self.run_helper('--apply', success=False).stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.touched().read_bytes(), self.before['external/project0', 'Android.bp'])

    def test_symlink_ancestor_is_rejected(self):
        original = self.tree / 'external'
        target = self.root / 'external'
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
        self.assertIn('Symlink path', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_patch_symlink_is_rejected(self):
        original = self.series_dir / self.records[1]['patch']
        target = self.root / 'patch-target'
        original.rename(target)
        original.symlink_to(target)
        self.assertIn('Symlink path', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_series_symlink_is_rejected(self):
        original = self.series
        target = self.root / 'series-target'
        original.rename(target)
        original.symlink_to(target)
        self.assertIn('Symlink path', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_paths_outside_workspace_are_rejected(self):
        for option, value in (('--tree', '/outside-rabbitr1/tree'),
                              ('--series', '/outside-rabbitr1/series.json')):
            self.assertIn('Path must be inside /rabbitr1',
                          self.run_helper('--apply', option, value, success=False).stderr)
        self.assert_original()

    def test_duplicate_project_is_rejected(self):
        self.records.append(self.records[0].copy())
        self.save()
        self.assertIn('Only one patch per project', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_mode_change_is_rejected(self):
        self.touched(1).chmod(0o755)
        self.assertIn('its mode changed', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_mode_patch_is_rejected(self):
        self.change_patch(1, lambda patch: patch + b'old mode 100644\nnew mode 100755\n')
        self.assertIn('Unsupported patch operation', self.run_helper('--apply', success=False).stderr)
        self.assert_original()

    def test_series_inside_git_subdirectory(self):
        nested = self.touched().parent / 'nested' / 'patches'
        nested.parent.mkdir()
        self.series_dir.rename(nested)
        self.series_dir = nested
        self.series = nested / 'series.json'
        self.run_helper('--apply')
        for (project, name), content in self.after.items():
            self.assertEqual((self.tree / project / name).read_bytes(), content)

    def test_invalid_record_is_rejected(self):
        self.records[1]['files'] = []
        self.save()
        self.assertIn('must declare its touched files', self.run_helper('--apply', success=False).stderr)
        self.assert_original()


if __name__ == '__main__':
    unittest.main(verbosity=2)
