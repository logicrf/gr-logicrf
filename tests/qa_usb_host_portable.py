"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
@file qa_usb_host_portable.py
@brief Check USB deployment payloads, accounts, paths, original locks and failure recovery.
@details Tests use temporary directories and mocks only. Native administrator installation,
         service rollback and actual USB reception have separate verification evidence.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-24
"""
import importlib.util
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('portable_host', Path(__file__).resolve().parents[1] / 'scripts/install_usb_host_portable.py')
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)


class PortableInputs(unittest.TestCase):
    """Exercise deployment inputs with small ELF identity fixtures; never execute the fixture binaries."""

    def setUp(self):
        """Create isolated build fixtures and output paths for this test."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.build = self.root / 'build with spaces'
        self.build.mkdir()
        for name in HOST.BINARIES:
            (self.build / name).write_bytes(b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'fixture')
        self.bundle = self.root / 'bundle'

    def tearDown(self):
        """Remove only this test-owned temporary directory."""
        self.temporary.cleanup()

    def prepared(self):
        """Freeze actual LogicRF source files with non-executable ELF fixtures and return the approval hash."""
        return HOST.prepare(self.build, self.bundle)['manifest_sha256']

    def test_complete_bundle_and_no_vendor_library(self):
        """Accept the complete fixed file set and exclude vendor libraries from the prepared bundle."""
        sha = self.prepared()
        manifest, files = HOST.read_bundle(self.bundle, sha)
        self.assertEqual(set(files), HOST.expected_members())
        self.assertEqual(manifest['external_library_sha256'], HOST.LIBRARY_SHA)
        self.assertFalse(any('libftd3xx' in name for name in files))

    def test_approved_manifest_required(self):
        """Reject manifests whose exact hash was not approved by the administrator."""
        self.prepared()
        with self.assertRaisesRegex(ValueError, 'APPROVED_MANIFEST'):
            HOST.read_bundle(self.bundle, '0' * 64)

    def test_payload_corruption(self):
        """Reject a payload changed after the manifest was approved."""
        sha = self.prepared()
        (self.bundle / 'private/usb_host_guard.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'BUNDLE_FILE_DRIFT'):
            HOST.read_bundle(self.bundle, sha)

    def test_extra_file(self):
        """Reject files outside the approved set even when all listed payloads match."""
        sha = self.prepared()
        (self.bundle / 'extra.py').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'EXTRA_OR_MISSING'):
            HOST.read_bundle(self.bundle, sha)

    def test_symlink_file(self):
        """Reject symlink payloads before privileged installation."""
        sha = self.prepared()
        path = self.bundle / 'public/logicrf-ft601-rx'
        target = self.build / 'logicrf-ft601-rx'
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'UNSAFE_BUNDLE'):
            HOST.read_bundle(self.bundle, sha)

    def test_existing_output_preserved(self):
        """Reject repeated preparation and preserve the original manifest bytes."""
        self.prepared()
        before = (self.bundle / 'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):
            HOST.prepare(self.build, self.bundle)
        self.assertEqual(before, (self.bundle / 'manifest.json').read_bytes())

    def test_path_controls_and_link_ancestor(self):
        """Allow ordinary spaces; reject systemd expansion, controls and symlink ancestors."""
        self.assertEqual(HOST.plain(self.build), self.build)
        for suffix in ['bad%u', 'bad\npath', '../bad']:
            with self.assertRaises(ValueError):
                HOST.plain(self.root / suffix)
        link = self.root / 'link'
        link.symlink_to(self.build, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'SYMLINK'):
            HOST.plain(link / 'target')

    def test_different_login_user(self):
        """Resolve the explicit user rather than assuming logicrf or UID 1000."""
        alice = SimpleNamespace(pw_uid=2107, pw_gid=2110, pw_shell='/bin/bash')
        with patch.object(HOST.pwd, 'getpwnam', return_value=alice) as lookup:
            self.assertEqual(HOST.account('alice').pw_uid, 2107)
            lookup.assert_called_once_with('alice')
        for uid, shell in [(0, '/bin/bash'), (999, '/bin/bash'), (65534, '/usr/sbin/nologin')]:
            with patch.object(HOST.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=uid, pw_shell=shell)):
                with self.assertRaisesRegex(ValueError, 'ORDINARY_LOGIN'):
                    HOST.account('system')

    def test_unit_quotes_spaces_and_uses_selected_uid(self):
        """Quote the selected paths and UID while preserving the fixed control endpoint."""
        root = self.root / 'install with spaces'
        (root / 'private').mkdir(parents=True)
        (root / 'private/usb_host_guard.py').write_text('guard fixture')
        text = HOST.unit_text(root, {'uid': 2107}).decode()
        self.assertIn('"--uid" "2107"', text)
        self.assertIn('"' + str(root / 'broker/usb_host_broker.py') + '"', text)
        self.assertIn('RuntimeDirectory=logicrf-h102-usb', text)
        self.assertNotIn('/data/logicrf/projects', text)

    def test_original_lock_required(self):
        """Reject parallel locks or drift in the registered original inode."""
        lock = self.root / 'original.lock'
        lock.write_bytes(b'')
        info = lock.stat()
        old = {'deployment': {'lock': str(lock), 'lock_identity': [info.st_dev, info.st_ino, info.st_uid], 'serial': 'S123'}}
        self.assertEqual(HOST.choose_lock(None, 'S123', old), (lock, False))
        with self.assertRaisesRegex(ValueError, 'EXISTING_LOCK_MUST'):
            HOST.choose_lock(self.root / 'another.lock', 'S123', old)
        old['deployment']['lock_identity'][1] += 1
        with self.assertRaisesRegex(ValueError, 'EXISTING_LOCK_DRIFT'):
            HOST.choose_lock(None, 'S123', old)

    def test_original_user_owned_private_lock(self):
        """Reuse the installed user's original 0600 lock without changing its ownership."""
        path = self.root / 'legacy.lock'
        path.write_bytes(b'')
        path.chmod(0o600)
        before = path.stat()
        info = HOST.lock_info(path, os.getuid())
        self.assertEqual((before.st_dev, before.st_ino, before.st_uid),
                         (info.st_dev, info.st_ino, info.st_uid))
        path.chmod(0o666)
        with self.assertRaisesRegex(PermissionError, 'PRIVATE_ORIGINAL_LOCK'):
            HOST.lock_info(path, os.getuid())

    def test_reused_default_lock_not_reported_as_new(self):
        """Preserve the original inode and report reuse rather than creation on a second selection."""
        with patch.object(HOST, 'DEFAULT_LOCK_DIRECTORY', self.root), \
                patch.object(HOST, 'LEGACY_LOCK', self.root/'missing-legacy.lock'), \
                patch.object(HOST, 'root_parents'), \
                patch.object(HOST, 'root_path', side_effect=lambda path, **kwargs: path):
            lock, created = HOST.choose_lock(None, 'FIRST', None)
            self.assertTrue(created)
            inode = lock.stat().st_ino
            same, created = HOST.choose_lock(None, 'FIRST', None)
            self.assertFalse(created)
            self.assertEqual(same.stat().st_ino, inode)

    def test_failed_service_start_restores_original_unit(self):
        """Restore the exact previous unit after a simulated service startup failure."""
        root = self.root / 'installation'
        root.mkdir()
        unit = self.root / 'logicrf-h102-usb-api.service'
        original = b'[Service]\nExecStart=/bin/true\n'
        unit.write_bytes(original)
        deployment = {'uid': 2107, 'lock_identity': [1, 2, 0]}
        state = {'active': 'active', 'enabled': 'enabled'}

        def system_command(*args):
            """Fail only the new enable --now operation and allow the modeled recovery commands."""
            if args[:3] == ('systemctl', 'enable', '--now'):
                raise subprocess.CalledProcessError(1, args)
            return ''

        with patch.object(HOST, 'UNIT', unit), patch.object(HOST.os, 'geteuid', return_value=0), \
                patch.object(HOST, 'installed', return_value=({}, deployment)), \
                patch.object(HOST, 'existing', return_value={'unit': original, 'deployment': deployment}), \
                patch.object(HOST, 'service_state', return_value=state), \
                patch.object(HOST, 'unit_text', return_value=b'[Service]\nExecStart=/bin/false\n'), \
                patch.object(HOST, 'idle', return_value=nullcontext()), \
                patch.object(HOST, 'command', side_effect=system_command):
            with self.assertRaises(subprocess.CalledProcessError):
                HOST.activate(root, HOST.digest(original), require_usb=False)
        self.assertEqual(unit.read_bytes(), original)
        self.assertEqual(json.loads((root / 'activation.json').read_text())['status'],
                         'FAILED_ORIGINAL_SERVICE_RESTORED')


if __name__ == '__main__':
    unittest.main()

# End of file: qa_usb_host_portable.py
# Contact: info@logicrf.com | Website: https://www.logicrf.com
