"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_install.py
@brief Detect in-use installation prefixes through actual subprocess mappings before overwrite.
@details Load temporary copies only, without flowgraphs/boards; in-use status clears after subprocess exit.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-17
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstallTests(unittest.TestCase):
    """Verify actual /proc mapping lifetime without rewriting loaded ELF files."""
    def test_active_library_prevents_install(self):
        from gnuradio import logicrf
        source=Path(__file__).resolve().parents[1]/'scripts/check_install_idle.py'
        spec=importlib.util.spec_from_file_location('install_idle_qa',source);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        libraries=[line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines() if '/libgnuradio-logicrf.so' in line]
        self.assertTrue(libraries)
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        with tempfile.TemporaryDirectory(prefix='install-qa-',dir=project/'tmp') as root:
            prefix=Path(root);library=prefix/'libgnuradio-logicrf.so';shutil.copyfile(libraries[0],library)
            process=subprocess.Popen([sys.executable,'-c','import ctypes,sys;ctypes.CDLL(sys.argv[1]);print("READY",flush=True);sys.stdin.read()',str(library)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'READY')
                self.assertIn(process.pid,module.users(prefix))
            finally:
                process.communicate(timeout=5)
            self.assertEqual(process.returncode,0);self.assertEqual(module.users(prefix),[])


if __name__=='__main__':unittest.main()
# End of file: qa_install.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
