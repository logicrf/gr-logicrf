"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_public_runtime.py
@brief Verify bundled control completeness, GUI independence and rejection of corrupted source.
@details Offline loading/file identity only, without H102 connections, ownership or RF permission.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.2.2
@date 2026-10-20
"""
import json
import hashlib
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gnuradio.logicrf.managed import default_runtime, load_runtime, _verified_runtime
from h102_control.source_identity import installed_sources


class PublicRuntimeTests(unittest.TestCase):
    """New customer workspaces need no private environment while retaining complete per-file SHA checks."""
    def setUp(self):
        self.lock=Path(__file__).resolve().parents[1]/'docs/h102-runtime.lock.json'

    def test_bundled_runtime_without_gui_or_network(self):
        """Load exactly fourteen files with networking forbidden and no standalone GUI framework imports."""
        with TemporaryDirectory() as temporary:
            project=Path(temporary);runtime=default_runtime(project,self.lock)
            self.assertNotIn(project,runtime.parents)
            self.assertEqual(len(json.loads(self.lock.read_text())['runtime_files']),14)
            self.assertFalse((runtime/'h102_iio_demo/gui.py').exists())
            with patch('paramiko.SSHClient',side_effect=AssertionError('SSH forbidden')), \
                 patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
                backend=load_runtime(runtime,self.lock)
            self.assertEqual(backend.CONTROL_TRANSPORT,'h102-api-v1')
            self.assertFalse(any(name.startswith(('PySide6','pyqtgraph','h102_iio_demo.gui')) for name in sys.modules))

    def test_tampered_and_missing_sources_rejected(self):
        """Corrupt/missing files fail before import/hardware without fallback to another environment."""
        with TemporaryDirectory() as temporary:
            copy=Path(temporary)/'runtime'
            shutil.copytree(default_runtime(Path(temporary),self.lock),copy)
            path=copy/'h102_iio_demo/backend.py';original=path.read_bytes()
            path.write_bytes(original+b'\n# tampered\n')
            with self.assertRaisesRegex(RuntimeError,'CONTROL_RUNTIME_SOURCE_MISMATCH'):
                _verified_runtime(copy,self.lock)
            path.write_bytes(original);path.unlink()
            with self.assertRaises(FileNotFoundError):_verified_runtime(copy,self.lock)

    def test_translated_helpers_request_existing_installed_sources(self):
        """English host copies still request the original, independently checked API36 helpers."""
        root=Path(__file__).resolve().parents[1]
        pins=json.loads((root/'python/h102_control/installed-helpers.json').read_text())
        local={name:hashlib.sha256((root/entry['local_path']).read_bytes()).hexdigest()
               for name,entry in pins['helpers'].items()}
        self.assertEqual(local,{name:entry['local_sha256'] for name,entry in pins['helpers'].items()})
        mapped=installed_sources(local)
        self.assertEqual(mapped['board_session.py'],'df4ca96578cde57692f0e5500a529e21d9b97a43d37b577b24284960bf8487d8')
        self.assertEqual(mapped['board_radio.py'],'fd5dec1a90f33177f5cd2230a4fc6572c155fec8292eaa9f86040c8c082e0e33')
        self.assertEqual(installed_sources(mapped),mapped)

    def test_edited_or_unknown_helpers_cannot_claim_reviewed_identity(self):
        """Changed bytes and unknown names cannot gain a pinned installed-helper identity."""
        altered={'board_session.py':'0'*64,'unknown.py':'1'*64}
        self.assertEqual(installed_sources(altered),altered)


if __name__=='__main__':unittest.main()
# End of file: qa_public_runtime.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
