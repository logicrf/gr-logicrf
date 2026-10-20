"""Isolate ordinary IP-only APIs from explicit administrator maintenance; offline construction never contacts devices."""
from dataclasses import fields
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from gnuradio.logicrf.managed import load_runtime,load_maintenance_runtime,default_runtime


class IpControlTests(unittest.TestCase):
    def test_api_and_explicit_maintenance_do_not_mutate_each_other(self):
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        lock=Path(__file__).resolve().parents[1]/'docs/h102-runtime.lock.json'
        runtime=default_runtime(project,lock)
        with patch('paramiko.SSHClient',side_effect=AssertionError('offline SSH forbidden')):
            admin=load_maintenance_runtime(runtime,lock)
            normal=load_runtime(runtime,lock)
            self.assertIsNot(admin,normal)
            self.assertEqual([f.name for f in fields(normal.Connection)],['host','port'])
            self.assertIn('password',[f.name for f in fields(admin.Connection)])
            self.assertEqual(normal.CONTROL_TRANSPORT,'h102-api-v1')
            self.assertFalse(hasattr(admin,'CONTROL_TRANSPORT'))
            self.assertIs(load_runtime(runtime,lock),normal)

if __name__=='__main__':unittest.main()
