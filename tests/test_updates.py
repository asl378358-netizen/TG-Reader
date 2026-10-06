import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import update_client as u


def package(version='0.3.1', *, invalid=False, requirements=b'offline-test'):
    files = {name: b'# offline test\n' for name in ('app.py', 'launcher.py', 'winlaunch.py', 'update_client.py')}
    files['requirements.txt'] = requirements
    manifest = {'schema': 1, 'version': version, 'files': {
        name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in files.items()}}
    if invalid: files['app.py'] = b'tampered'
    files['update-manifest.json'] = json.dumps(manifest).encode()
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as z:
        for name, data in files.items(): z.writestr('owner-repo-sha/' + name, data)
    return raw.getvalue(), manifest


class FakeGitHub:
    def __init__(self, data, sha='b' * 40): self.data, self.sha, self.calls = data, sha, []
    def request(self, path, *, binary=False):
        self.calls.append(path)
        return self.data if binary else {'sha': self.sha}


class UpdateTests(unittest.TestCase):
    def state(self, base):
        root = Path(base)
        old = root / 'versions' / ('a' * 40); old.mkdir(parents=True)
        (old / 'app.py').write_bytes(b'old-app')
        current = {'sha': 'a' * 40, 'version': '0.3.0', 'python': '/offline/python',
                   'requirements_sha256': hashlib.sha256(b'offline-test').hexdigest()}
        u.save_json(root / 'current.json', current)
        (root / 'start.py').write_bytes(b'old-start')
        (root / 'config.json').write_bytes(b'keep-config')
        (root / 'messages.sqlite3').write_bytes(b'keep-messages')
        (root / 'desktop_test.auth.enc').write_bytes(b'keep-auth')
        return root, current

    def test_update_activates_only_after_validation_and_preserves_local_data(self):
        data, _ = package(); api = FakeGitHub(data); seen = []
        with tempfile.TemporaryDirectory() as temp:
            root, old = self.state(temp)
            def check(stage, python):
                self.assertEqual(json.loads((root / 'current.json').read_text()), old)
                self.assertEqual(Path(python), Path('/offline/python')); seen.append(stage)
            current, source, changed = u.check_update(root, client=api, validator=check)
            self.assertTrue(changed); self.assertEqual(len(seen), 1)
            self.assertEqual(current['sha'], 'b' * 40)
            self.assertEqual(current['previous'], old['sha'])
            self.assertEqual((root / 'start.py').read_bytes(), b'# offline test\n')
            self.assertEqual((root / 'config.json').read_bytes(), b'keep-config')
            self.assertEqual((root / 'messages.sqlite3').read_bytes(), b'keep-messages')
            self.assertEqual((root / 'desktop_test.auth.enc').read_bytes(), b'keep-auth')
            self.assertEqual((root / 'versions' / old['sha'] / 'app.py').read_bytes(), b'old-app')

    def test_current_version_does_not_download_or_install_again(self):
        api = FakeGitHub(b'', sha='a' * 40)
        with tempfile.TemporaryDirectory() as temp:
            root, _ = self.state(temp)
            _, _, changed = u.check_update(root, client=api, validator=lambda *_: self.fail('validator ran'))
        self.assertFalse(changed); self.assertEqual(api.calls, ['commits/stable'])

    def test_tampered_archive_keeps_old_pointer_and_start(self):
        data, _ = package(invalid=True)
        with tempfile.TemporaryDirectory() as temp:
            root, old = self.state(temp)
            with self.assertRaises(u.UpdateError): u.check_update(root, client=FakeGitHub(data), validator=lambda *_: None)
            self.assertEqual(json.loads((root / 'current.json').read_text()), old)
            self.assertEqual((root / 'start.py').read_bytes(), b'old-start')

    def test_failed_runtime_check_does_not_activate(self):
        data, _ = package()
        with tempfile.TemporaryDirectory() as temp:
            root, old = self.state(temp)
            def fail(*_): raise u.UpdateError('failed import')
            with self.assertRaises(u.UpdateError): u.check_update(root, client=FakeGitHub(data), validator=fail)
            self.assertEqual(json.loads((root / 'current.json').read_text()), old)
            self.assertFalse((root / 'versions' / ('b' * 40)).exists())

    def test_activation_write_failure_restores_pointer_and_launcher(self):
        data, _ = package()
        with tempfile.TemporaryDirectory() as temp:
            root, old = self.state(temp)
            stage = root / 'stage'; u.unpack_archive(data, stage)
            with patch.object(u, 'save_json', side_effect=OSError('simulated disk error')):
                with self.assertRaises(OSError): u.activate(root, {**old, 'sha': 'b' * 40}, stage)
            self.assertEqual(json.loads((root / 'current.json').read_text()), old)
            self.assertEqual((root / 'start.py').read_bytes(), b'old-start')

    def test_zip_traversal_links_case_collisions_and_oversize_are_rejected(self):
        for name in ('repo/../escape.py', 'repo/../../escape.py', '/absolute', 'repo/C:/evil', 'repo\\evil'):
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as z:
                info = zipfile.ZipInfo('placeholder'); info.filename = name
                z.writestr(info, b'test')
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(u.UpdateError): u.unpack_archive(raw.getvalue(), Path(temp) / 'stage')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as z:
            link = zipfile.ZipInfo('repo/link'); link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, b'../../escape')
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(u.UpdateError): u.unpack_archive(raw.getvalue(), temp)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as z:
            z.writestr('repo/app.py', b'a'); z.writestr('repo/APP.py', b'b')
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(u.UpdateError): u.unpack_archive(raw.getvalue(), temp)
        with tempfile.TemporaryDirectory() as temp, patch.object(u, 'MAX_ARCHIVE', 1):
            data, _ = package()
            with self.assertRaises(u.UpdateError): u.unpack_archive(data, temp)

    def test_concurrent_updater_cannot_change_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            root, _ = self.state(temp)
            with u.update_lock(root):
                code = "import sys; import update_client as u\ntry:\n with u.update_lock(sys.argv[1]): sys.exit(2)\nexcept u.UpdateError: sys.exit(0)"
                child = subprocess.run([sys.executable, '-c', code, str(root)],
                    cwd=Path(u.__file__).parent, capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0, child.stderr)

    def test_new_dependencies_use_separate_environment_not_old_python(self):
        with tempfile.TemporaryDirectory() as temp:
            root, old = self.state(temp)
            manifest = {'requirements_sha256': 'c' * 64}
            result = subprocess.CompletedProcess([], 0, '/existing/python.exe\n', '')
            with patch.object(u, 'run_capture', return_value=result) as run:
                python = u.prepare_python(root, root / 'source', manifest, old, lambda _: None)
            self.assertIn('envs', python.parts)
            self.assertTrue(any('-m' in call.args[0] and 'venv' in call.args[0] for call in run.call_args_list))
            self.assertTrue(any('-r' in call.args[0] for call in run.call_args_list))
            self.assertNotEqual(str(python), old['python'])


class AccessTests(unittest.TestCase):
    def test_gh_credentials_are_captured_without_login_or_browser(self):
        result = subprocess.CompletedProcess([], 0, 'secret-from-test-store\n', '')
        with patch.dict(os.environ, {'GH_TOKEN': '', 'GITHUB_TOKEN': ''}), \
             patch.object(u.shutil, 'which', side_effect=lambda name: '/tools/gh' if name == 'gh' else None), \
             patch.object(u, 'run_capture', return_value=result) as run:
            self.assertEqual(u.existing_token(), 'secret-from-test-store')
        self.assertEqual(run.call_args.args[0], ['/tools/gh', 'auth', 'token', '--hostname', 'github.com'])

    def test_git_credential_manager_is_asked_noninteractively(self):
        result = subprocess.CompletedProcess([], 0, 'username=test\npassword=test-secret\n', '')
        with patch.dict(os.environ, {'GH_TOKEN': '', 'GITHUB_TOKEN': ''}), \
             patch.object(u.shutil, 'which', side_effect=lambda name: '/tools/git' if name == 'git' else None), \
             patch.object(u, 'run_capture', return_value=result) as run:
            self.assertEqual(u.existing_token(), 'test-secret')
        self.assertIn('credential.interactive=never', run.call_args.args[0])
        self.assertIn('host=github.com', run.call_args.kwargs['input'])
        self.assertEqual(u.child_environment()['GCM_INTERACTIVE'], 'never')

    def test_public_repo_needs_no_token_lookup(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return b'{"sha":"test"}'
        with patch.object(u.urllib.request, 'urlopen', return_value=Response()), \
             patch.object(u, 'existing_token', side_effect=AssertionError('credential lookup')):
            self.assertEqual(u.GitHubClient().request('commits/stable'), {'sha': 'test'})

    def test_missing_private_access_is_safe_and_does_not_open_browser(self):
        error = urllib.error.HTTPError('https://api.github.com/test', 404, 'private', {}, None)
        with patch.object(u.urllib.request, 'urlopen', side_effect=error), \
             patch.object(u, 'existing_token', return_value=None):
            with self.assertRaisesRegex(u.UpdateError, 'приватный'): u.GitHubClient().request('commits/stable')

    def test_auth_error_does_not_print_token(self):
        error = urllib.error.HTTPError('https://api.github.com/test', 401, 'private', {}, None)
        with patch.object(u.urllib.request, 'urlopen', side_effect=error), \
             patch.object(u, 'existing_token', return_value='private-test-secret'):
            with self.assertRaises(u.UpdateError) as raised: u.GitHubClient().request('commits/stable')
        self.assertNotIn('private-test-secret', str(raised.exception))


if __name__ == '__main__': unittest.main()
