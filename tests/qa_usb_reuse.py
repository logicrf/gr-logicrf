"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_reuse.py
@brief Offline construction, legacy STOP and partial-commit recovery models for the new USB RX entry.
@details External per-SHA SDK generates actual code; run register models only, without devices.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from threading import Event
from concurrent.futures import ThreadPoolExecutor

from gnuradio.logicrf.usb_rx_reuse_candidate import build


class UsbReuseTests(unittest.TestCase):
    """Models establish control/rejection only; measure end-to-end data/latency separately."""
    def test_post_barrier_overlaps_independent_work_and_waits_for_both(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                profile='2msps-0p25mhz',sample_rate=2000000)
        c=board.controller;c._save=Mock();c.host_launch=dict(capabilities={'epoch_archive_v1':True})
        host_entered=Event();audit_done=Event();host_release=Event()
        def host(epoch):
            host_entered.set()
            if not host_release.wait(2):raise TimeoutError('model host release missing')
        def audit(controller,epoch):
            self.assertIs(controller,c);self.assertEqual(epoch,7)
            if not host_entered.wait(2):raise TimeoutError('independent work was serialized')
            audit_done.set()
        c._finish_host_receipt=host
        from unittest.mock import patch
        try:
            with patch('gnuradio.logicrf.epoch_evidence.checkpoint_audit',side_effect=audit), ThreadPoolExecutor(1) as pool:
                future=pool.submit(c._finish_host_epoch,7)
                self.assertTrue(audit_done.wait(1));self.assertFalse(future.done())
                host_release.set();future.result(2)
            for endpoint in ('host','audit'):
                c._finish_host_receipt=Mock(side_effect=RuntimeError('host failure') if endpoint=='host' else None)
                with patch('gnuradio.logicrf.epoch_evidence.checkpoint_audit',side_effect=RuntimeError('audit failure') if endpoint=='audit' else None) as checked:
                    with self.subTest(endpoint=endpoint), self.assertRaisesRegex(RuntimeError,'USB_EPOCH_POST_BARRIER'):
                        c._finish_host_epoch(8)
                    checked.assert_called_once();c._finish_host_receipt.assert_called_once()
        finally:
            host_release.set();c.finished=True;c.finish_errors=[];board.close()

    def test_candidate_bit_requires_exact_same_boot_uart_loading_proof(self):
        from copy import deepcopy
        from gnuradio.logicrf.usb_managed import verify_loaded_bit
        policy = dict(bit_path='/boot/h102-hot-metadata-709b2d8.bit', bit_sha256='a'*64,
                      uart_raw_sha256='b'*64, boot_id='candidate-boot',
                      compiled_commit='c'*40, routed_sha256='d'*64)
        pins = dict(boot_pins={policy['bit_path']:policy['bit_sha256']}, active_bit=policy)
        identity = dict(boot_id=policy['boot_id'],machine_id='model-machine',MAC='02:00:00:00:00:01')
        identity['loaded_bit'] = dict(schema=1,**identity,bit_path=policy['bit_path'],
                                     bit_sha256=policy['bit_sha256'],uart_raw_sha256=policy['uart_raw_sha256'])
        verify_loaded_bit(identity,pins)
        # A matching file/capability is insufficient when the loading proof is
        # absent, from another boot, altered, or belongs to another board.
        for field,value in (('boot_id','old-boot'),('machine_id','other-machine'),
                            ('bit_sha256','e'*64),('uart_raw_sha256','f'*64),('schema',True)):
            changed=deepcopy(identity);changed['loaded_bit'][field]=value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError,'USB_CANDIDATE_BIT_LOADING_PROOF'):
                verify_loaded_bit(changed,pins)
        missing=deepcopy(identity);del missing['loaded_bit']
        with self.assertRaisesRegex(RuntimeError,'USB_CANDIDATE_BIT_LOADING_PROOF'):
            verify_loaded_bit(missing,pins)
        changed=deepcopy(identity);changed['boot_id']='new-boot';changed['loaded_bit']['boot_id']='new-boot'
        with self.assertRaisesRegex(RuntimeError,'USB_CANDIDATE_BIT_LOADING_PROOF'):
            verify_loaded_bit(changed,pins)
        wrong=deepcopy(pins);wrong['boot_pins'][policy['bit_path']]='e'*64
        with self.assertRaisesRegex(RuntimeError,'USB_CANDIDATE_BIT_POLICY_PIN'):
            verify_loaded_bit(identity,wrong)
        # Existing formal packages have no one-boot candidate policy.
        verify_loaded_bit({},dict(boot_pins={}))

    def test_negotiated_stop_uses_one_verified_host_call_and_is_idempotent(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                profile='2msps-0p25mhz',sample_rate=2000000)
        c=board.controller;c._save=Mock()
        c.host_launch=dict(capabilities={'rx_stop_ack_v1':True})
        proof=dict(ok=True,state='STOPPED',rx_control=0,tx_control=0)
        c._command=Mock(return_value=proof)
        c._host=Mock(return_value=dict(sent='stop_ack',board_stop_verified=True,helper_stop_observed=True))
        try:
            c._stop_pair();c._stop_pair()
            c._command.assert_called_once_with('stop')
            c._host.assert_called_once_with('send',dict(op='stop_ack',proof=proof))
            self.assertIn('host_stop_ack',c.guard_records)
        finally:
            c.finished=True;c.finish_errors=[];board.close()

    def test_initial_cached_prepare_uses_same_metadata_fields_as_hot_epoch(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                profile='2msps-0p25mhz',sample_rate=2000000)
        c=board.controller;c.registered['rx']={1,2};c.prepared_options=board.options()
        c.prepared_options.metadata_json=json.dumps(dict(configuration_id=149))
        c.prepared_options.usb_time_epoch=145;c.expected=dict(configuration_id=147)
        try:
            actual=json.loads(c.prepare_direction('rx',3).metadata_json)
            self.assertEqual(actual,dict(configuration_id=149,usb_configuration_id=147,time_epoch=145))
        finally:
            c.finished=True;c.finish_errors=[];board.close()

    def test_constructor_is_no_io_and_rx_only(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board = make_grc_rx_device(backend='usb', host='192.0.2.1', channels=(1,2),
                                  profile='2msps-0p25mhz', sample_rate=2000000)
        self.assertTrue(board.controller.reuse_board)
        self.assertFalse(board.controller.hold_for_tune)
        self.assertIsNone(board.controller.connection)
        self.assertIsNone(board.controller.run_output)
        from gnuradio import gr
        from gnuradio.logicrf.grc_session import GrcRxSession
        session=GrcRxSession(gr.top_block(),board)
        self.assertTrue(session.tune(100100000))
        self.assertIsNone(board.controller.connection)
        self.assertEqual(board.options().center_frequency,100100000)
        board.close()
        with self.assertRaisesRegex(ValueError, 'USB_REUSE_RX_ONLY'):
            build(Path('/unused'), {}, radio_plan=dict(rf_enabled=True, tx_channels=[1]),
                  reuse_source='')

    def test_host_post_is_async_but_next_epoch_waits_for_it(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                profile='2msps-0p25mhz',sample_rate=2000000)
        c=board.controller;c._save=Mock();c.hold_for_tune=True;c.board_stopped=True
        c.guard_records={'stop':{'ok':True}};c.expected={};c.prepared_options=board.options()
        entered=Event();release=Event()
        def finish(epoch):
            self.assertEqual(epoch,0);entered.set()
            if not release.wait(2):raise TimeoutError('test release missing')
        c._finish_host_epoch=finish
        try:
            c.finish_direction('rx')
            self.assertTrue(entered.wait(1));self.assertTrue(c.paused);self.assertTrue(c.host_post_pending)
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(c._join_host_post)
                self.assertFalse(future.done())
                release.set();future.result(2)
            self.assertFalse(c.host_post_pending)
        finally:
            release.set()
            if c.host_finisher:c.host_finisher.join(2)
            c.finished=True;c.finish_errors=[];board.close()

    def test_post_failure_after_eos_invalidates_paused_receiver(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                profile='2msps-0p25mhz',sample_rate=2000000)
        c=board.controller;c._save=Mock();c.hold_for_tune=True;c.board_stopped=True
        c.guard_records={'stop':{'ok':True}};c.expected={};c.prepared_options=board.options()
        c._finish_host_epoch=Mock(side_effect=RuntimeError('injected_post_failure'))
        try:
            c.finish_direction('rx');c.host_finisher.join(2)
            self.assertFalse(c.host_finisher.is_alive());self.assertTrue(c.paused)
            self.assertIn('injected_post_failure',board.error())
            with self.assertRaisesRegex(RuntimeError,'injected_post_failure'):c._join_host_post()
        finally:
            c.finished=True;c.finish_errors=[]
            with self.assertRaisesRegex(RuntimeError,'injected_post_failure'):board.close()

    def test_hot_metadata_keeps_managed_and_usb_configuration_ids_separate(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        with TemporaryDirectory() as temporary:
            board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                                    profile='2msps-0p25mhz',sample_rate=2000000)
            c=board.controller;c.run_output=Path(temporary);c.paused=True;c.board_stopped=True
            c.prepared_options=board.options();c.prepared_options.usb_time_epoch=147
            c.expected=dict(rx_mask=3,tx_mask=0,device_id='1234',configuration_id=147,rx_stream_id='41')
            raw=bytes(112);prepared=dict(c.expected,configuration_id=148,rx_stream_id='51',
                                        rx_sha256=hashlib.sha256(raw).hexdigest())
            c.info=dict(configuration_id=149,radio={})
            c._command=Mock(return_value=dict(state='CONFIGURED',epoch_info=dict(prepared=prepared,
                configuration_hex=raw.hex(),time_epoch=148,native_time=dict(time_epoch=148,failed=False,observations=1),
                previous_stopped_not_restarted=True,radio=dict(rx_center_frequency_hz={'1':100100000,'2':100100000}))))
            c._host=Mock(return_value={'launched':True})
            try:
                c.retune(100100000)
                info=json.loads(c.prepared_options.metadata_json)
                self.assertEqual(info['configuration_id'],149)
                self.assertEqual(info['usb_configuration_id'],148)
                self.assertEqual(info['time_epoch'],148)
            finally:
                c.host_launch=None;c.finished=True;c.finish_errors=[];board.close()

    def test_rendered_state_and_recovery_model(self):
        project = Path(os.environ['LOGICRF_PROJECT_ROOT'])
        sdk = project/'envs/h102-sdk-runtime-input/1.2.0-grc-hotmeta-1'
        if not sdk.is_dir():self.skipTest('external pinned H102 SDK source input not installed')
        repo = Path(__file__).resolve().parents[1]
        lock = json.loads((repo/'docs/h102-usb-sdk.lock.json').read_text())
        runtime = project/'envs/h102-control-runtime/f782657/h102_iio_demo'
        extras = {name:(runtime/name).read_text() for name in ('board_tx.py','board_radio.py','tx_cyclic.py')}
        extras['gr_usb_radio.py'] = (repo/'python/logicrf/board_usb_radio.py').read_text()
        plan = dict(configuration='2msps-0p25mhz',rf_enabled=False,rx_channels=[1,2],tx_channels=[],
            rx_frequency=2400000000,tx_frequency=2400000000,attenuation_db=40,rx_gain_db={'1':20,'2':20})
        with TemporaryDirectory(prefix='usb-reuse-', dir=project/'tmp') as temporary:
            candidate = Path(temporary)/'code'
            report = build(candidate, lock, sdk, offline=True, radio_plan=plan, extra_sources=extras,
                           reuse_source=(repo/'python/logicrf/gr_usb_reuse.py').read_text())
            self.assertTrue(report['reusable_rx'])
            for file in candidate.glob('*.py'):compile(file.read_text(),file.name,'exec')
            for fixture in ('usb_reuse_model.py','usb_metadata_model.py'):
                result = subprocess.run([sys.executable, '-B', str(repo/'tests/fixtures'/fixture),
                                         str(candidate)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'],'PASS_MODEL_ONLY')

    def test_retained_time_requires_negotiated_dual_ack_and_fresh_observation(self):
        """Same-time-epoch acceptance requires new RTL negotiation/full actual evidence; missing fields never downgrade checks."""
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        with TemporaryDirectory() as temporary:
            for fault in (None,'old_capability','missing_flag','missing_ack','stale_observation'):
                with self.subTest(fault=fault):
                    board=make_grc_rx_device(backend='usb',host='192.0.2.1',channels=(1,2),
                        profile='2msps-0p25mhz',sample_rate=2000000)
                    c=board.controller;c.run_output=Path(temporary);c.paused=True;c.board_stopped=True
                    c.ready=dict(rx_metadata_capability=1 if fault=='old_capability' else 3)
                    c.prepared_options=board.options();c.prepared_options.usb_time_epoch=145
                    c.expected=dict(rx_mask=3,tx_mask=0,device_id='1234',configuration_id=147,rx_stream_id='41')
                    raw=bytes(112);prepared=dict(c.expected,configuration_id=148,rx_stream_id='51',
                        rx_sha256=hashlib.sha256(raw).hexdigest())
                    info=dict(prepared=prepared,configuration_hex=raw.hex(),time_epoch=145,
                        native_time=dict(time_epoch=145,failed=False,observations=12),
                        previous_stopped_not_restarted=True,radio=dict(rx_center_frequency_hz={'1':100100000,'2':100100000}),
                        time_retained_after_dual_native_metadata_commit=fault!='missing_flag',
                        metadata_commit=dict(abi=1,capability=3,status=0x74,dirty=0x1ff,
                            configuration_id=148,time_epoch=145,previous_observations=10,observations=12))
                    if fault=='missing_ack':info['metadata_commit']['status']=0x64
                    if fault=='stale_observation':info['metadata_commit']['previous_observations']=12
                    c.info=dict(configuration_id=149,radio={})
                    c._command=Mock(return_value=dict(state='CONFIGURED',epoch_info=info))
                    c._host=Mock(return_value={'launched':True})
                    try:
                        if fault:
                            with self.assertRaisesRegex(RuntimeError,'USB_REUSE_'):c.retune(100100000)
                            c._host.assert_not_called()
                        else:
                            c.retune(100100000)
                            self.assertEqual(c.prepared_options.usb_time_epoch,145)
                            self.assertEqual(c.prepared_options.usb_stream_id,51)
                    finally:
                        c.host_launch=None;c.finished=True;c.finish_errors=[];board.close()


if __name__ == '__main__':unittest.main()
# End of file: qa_usb_reuse.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
