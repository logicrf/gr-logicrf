"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_user_install.py
@brief Verify install traversal/extra-member rejection and actual GIO desktop argument decoding.
@details No devices/menu changes; temporary desktop launches write arguments to temporary files only.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/install_user.py'
spec=importlib.util.spec_from_file_location('_user_install',SCRIPT)
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class UserInstallTests(unittest.TestCase):
    """Installation boundary checks; actual full-module import/desktop Companion acceptance remains separate."""
    def test_dependency_stdout_is_not_a_protocol(self):
        good='LOGICRF_PREFLIGHT_JSON={"module":"/new/module","samples":12345}'
        self.assertEqual(installer.probe_identity('library diagnostic\n'+good+'\n')['samples'],12345)
        for bad in ('only diagnostic',good+'\n'+good):
            with self.assertRaises(RuntimeError):installer.probe_identity(bad)

    def test_exact_tree_rejects_extras_corruption_and_escape(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);file=root/'source.py';file.write_text('source')
            expected=installer.tree(root);installer.verify(root,expected)
            file.write_text('damaged')
            with self.assertRaises(ValueError):installer.verify(root,expected)
            file.write_text('source');extra=root/'unexpected.py';extra.write_text('extra')
            with self.assertRaises(ValueError):installer.verify(root,expected)
            extra.unlink();(root/'outside').symlink_to('/etc/hostname')
            with self.assertRaises(ValueError):installer.tree(root)
            with self.assertRaises(ValueError):installer.verify(root,{'../escape':{}})

    def test_desktop_exec_literal_arguments_use_actual_gio(self):
        import gi
        gi.require_version('Gio','2.0')
        from gi.repository import Gio
        with TemporaryDirectory() as temporary:
            root=Path(temporary);folder=root/'café name " \\ $ %';folder.mkdir()
            script=folder/'write args.py';output=folder/'result.json'
            script.write_text('import sys,json\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n')
            values=['one two','literal$','back\\slash','quote"here','percentage%here']
            command='/usr/bin/python3 '+' '.join(installer.desktop_argument(x) for x in [script,output,*values])
            desktop=root/'entry.desktop';desktop.write_text('[Desktop Entry]\nType=Application\nName=Test\nExec='+command+'\n')
            app=Gio.DesktopAppInfo.new_from_filename(str(desktop))
            self.assertIsNotNone(app);self.assertTrue(app.launch([],None))
            deadline=time.monotonic()+3
            while not output.exists():
                if time.monotonic()>deadline:self.fail('GIO output timeout')
                time.sleep(.01)
            self.assertEqual(json.loads(output.read_text()),values)


if __name__ == '__main__':unittest.main()
# End of file: qa_user_install.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
