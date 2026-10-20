"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_startup_warmup.py
@brief Check idle warmup never starts ownership early and one profile selection avoids duplicate status.
@details Bounded mocks verify failure, unclaimed cleanup and claimed ownership transfer through actual calls.
         These do not replace hardware ADC/data integrity/preparation-latency checks.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gnuradio.logicrf.idle_warmup import IdleWarmup
from gnuradio.logicrf.iio_tx_managed import IioTxController
from h102_control.compat import install


class FakeLink:
    """Record warm/close without a start method so mocks cannot hide premature startup."""
    def __init__(self, error=None):
        self.active=False
        self.warmed=Event()
        self.closed=[]
        self.error=error

    def warm(self):
        self.warmed.set()
        if self.error:raise self.error
        return self

    def close(self, stop=True):
        self.closed.append(stop)


class StartupWarmupTests(unittest.TestCase):
    """Synchronize events rather than flaky extremely short wall-clock assertions."""
    def test_warm_is_parallel_but_never_starts_owner(self):
        link=FakeLink();job=IdleWarmup(link)
        self.assertTrue(link.warmed.wait(2))
        self.assertFalse(link.active)
        info={'boot_id':'verified'};request={'info':info}
        self.assertIs(job.claim(info,request),link)
        self.assertIs(link.info,info)
        self.assertIs(link.expected(),request)
        job.close()
        self.assertEqual(link.closed,[])

    def test_failed_identity_or_cancel_closes_unclaimed_pipe(self):
        link=FakeLink();job=IdleWarmup(link)
        job.close()
        self.assertTrue(link.warmed.is_set())
        self.assertEqual(link.closed,[False])

    def test_warm_failure_is_not_retried_or_claimed(self):
        link=FakeLink(RuntimeError('bad source'));job=IdleWarmup(link)
        with self.assertRaisesRegex(RuntimeError,'bad source'):job.claim({}, {})
        job.close()
        self.assertFalse(job.claimed)
        self.assertEqual(link.closed,[False])

    def test_prepare_rejects_wrong_firmware_and_closes_parallel_pipe(self):
        """Execute actual prepare failures to cover finally wiring beyond the standalone warmup class."""
        class Native:
            def options(self):
                return SimpleNamespace(sample_rate=4000000,tx_samples=0,center_frequency=1000000000)
        native=Native()
        with TemporaryDirectory() as directory:
            root=Path(directory)
            lock=root/'firmware.json'
            lock.write_text(json.dumps(dict(boot_pins={'bit':'expected'})))
            controller=IioTxController(native,'192.0.2.1','',root,root,root,
                lock,root/'evidence','4msps-2mhz-common',None)
            controller.parallel_idle_warmup=True
            link=FakeLink()
            backend=SimpleNamespace(CONTROL_TRANSPORT='h102-api-v1',
                Connection=lambda host:SimpleNamespace(host=host),
                inspect=lambda connection:dict(actual=dict(identity=dict(boot_pins={'bit':'wrong'}))))
            with patch('gnuradio.logicrf.iio_tx_managed.load_runtime',return_value=backend), \
                    patch.object(controller,'_new_link',return_value=(link,{})):
                with self.assertRaisesRegex(RuntimeError,'FIRMWARE_BOOT_PINS_MISMATCH'):
                    controller.prepare(3)
            self.assertEqual(link.closed,[False])
            self.assertIsNone(controller.link)
            self.assertIsNone(controller.monitor)
            self.assertFalse(controller.recovery_confirmed)

    def test_profile_selection_uses_same_checked_boot_without_extra_status(self):
        class Session:
            pass
        backend=SimpleNamespace();install(backend,SimpleNamespace(SessionLink=Session))
        state={'info':{'active':'old','boot_id':'actual-boot','api':'68.21.6'}}
        selected=deepcopy(state);selected['info']['active']='new'
        with patch('h102_control.compat.Client') as client:
            client.return_value.status.side_effect=AssertionError('duplicate status')
            client.return_value.request.return_value=selected
            result=backend.inspect(SimpleNamespace(), 'new', current_state=state)
            client.return_value.request.assert_called_once_with('profile.select',
                dict(profile='new',boot_id='actual-boot'),timeout=240)
            self.assertEqual(result['info']['active'],'new')
            self.assertEqual(state['info']['active'],'old')


if __name__=='__main__':unittest.main()
# End of file: qa_startup_warmup.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
