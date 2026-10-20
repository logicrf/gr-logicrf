"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_fm_owner_scan.py
@brief Check same-owner FM sweep admission, finite DMA recovery and host cancellation boundaries.
@details Substitutes execute actual board-control functions without devices or RF/performance qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import copy
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from threading import Event
from unittest.mock import Mock, patch


class ScanTests(unittest.TestCase):
    """Actual entry functions/recovery models; full hardware/GRC acceptance is separate."""
    def setUp(self):
        self.capture=SimpleNamespace()
        self.radio=Mock()
        self.common=Mock(radio=self.radio)
        spec=importlib.util.spec_from_file_location('tested_scan',Path(__file__).resolve().parents[1]/'python/logicrf/board_fm_scan_stream.py')
        self.m=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,board_capture=self.capture,board_tx=self.common):spec.loader.exec_module(self.m)
        self.plan=dict(start_hz=87800000,stop_hz=108200000,sample_rate_hz=1536000,
                       bandwidth_hz=500000,fft_points=8192,averages=4,settle_ms=2,repeat=False,grid_size=32768)
        self.expected=dict(rx_channels=[1],channels=[],rate=1536000,rf_enabled=False)

    def test_plan_rejects_invalid_types_profiles_and_unbounded_batch(self):
        self.m.validate_scan(self.plan,[100100000],self.expected)
        for key,value in [('fft_points',True),('averages',32),('sample_rate_hz',2000000),
                          ('repeat',True),('stop_hz',5800000000),('grid_size',65536)]:
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'FM_SCAN_PLAN'):
                self.m.validate_scan(dict(self.plan,**{key:value}),[100100000],self.expected)
        for channels in ([1,2],[]):
            with self.assertRaises(ValueError):self.m.validate_scan(self.plan,[100100000],dict(self.expected,rx_channels=channels))
        with self.assertRaises(ValueError):self.m.validate_scan(self.plan,[100100000]*15,self.expected)

    def fixture(self,directory):
        hw=Mock();hw.phy=directory/'phy';hw.devices={'axi-adrv9002-rx-lpc':directory/'rx1'}
        key=str(hw.devices['axi-adrv9002-rx-lpc']/'buffer/enable')
        freq=str(directory/'frequency');hw.alt.return_value=Path(freq)
        settings={freq:'98100000',key:'0','gain':'20','tx':'off'}
        hw.settings.side_effect=lambda:dict(settings)
        hw.read.return_value='off'
        hw.write.side_effect=lambda p,v,*unused:settings.update({str(p):v})
        c=Mock();c.STATE=directory/'profile-state';c.read_json.return_value={};c.snapshot.return_value={'profile':'frozen'}
        rx=Mock();guard=SimpleNamespace(windows={1:dict(stage='ended',passed=True)})
        self.radio.describe.return_value=dict(groups=[dict(channels=[1,2],editable=True,tx_shared=[])],
            rx_center_frequency_hz={'1':98100000,'2':98100000})
        self.radio.desired_settings.side_effect=lambda before,hw,channels,f:dict(before,**{freq:str(f)})
        def apply(hw,before,channels,f,check):
            check();settings[freq]=str(f);return self.radio.desired_settings(before,hw,channels,f)
        self.radio.apply_carrier.side_effect=apply
        local=Mock();local.offsets=[];local.last_timings={}
        local.power.side_effect=lambda avg,check:(check(),settings.update({key:'1'}),[1.,2.])[2]
        local.close.side_effect=lambda:settings.update({key:'0'})
        self.capture.LocalCapture=Mock(return_value=local)
        self.capture.grid_cells=Mock(return_value=dict(grid_first=0,power_f32le_b64='AAAAAA=='))
        request=dict(actual={'profile':'frozen','parameters':{'rx':[dict(channel=1,sample_rate_hz=1536000,rf_bandwidth_hz=500000)]}})
        c.snapshot.return_value=request['actual']
        return hw,c,rx,guard,local,settings,request

    def test_live_guard_refused_before_any_hardware_write(self):
        with TemporaryDirectory() as t:
            d=Path(t);hw,c,rx,g,l,s,r=self.fixture(d);g.windows[1]['stage']='armed'
            with self.assertRaisesRegex(RuntimeError,'REQUIRES_ENDED'):
                self.m.scan_command('sweep_setup',dict(plan=self.plan,centers=[]),hw,dict(s),g,self.expected,r,d,{},c,rx)
            hw.write.assert_not_called();self.capture.LocalCapture.assert_not_called()

    def test_scan_restores_lo_buffer_and_preserves_ended_guard(self):
        with TemporaryDirectory() as t:
            d=Path(t);hw,c,rx,g,l,s,r=self.fixture(d);active=dict(s);original=dict(s);scan={}
            for action,command in [('sweep_setup',dict(plan=self.plan,centers=[])),
                                   ('sweep_batch',dict(plan=self.plan,centers=[100100000,107900000])),
                                   ('sweep_finish',{})]:
                result,active=self.m.scan_command(action,command,hw,active,g,self.expected,r,d,scan,c,rx)
            self.assertTrue(result['scan_restored']);self.assertTrue(result['local_dma_closed'])
            self.assertEqual(s,original);self.assertEqual(active,original);self.assertEqual(scan,{})
            self.assertEqual(g.windows[1]['stage'],'ended');l.close.assert_called_once()

    def test_foreign_settings_fail_batch_and_scan_remains_recoverable(self):
        with TemporaryDirectory() as t:
            d=Path(t);hw,c,rx,g,l,s,r=self.fixture(d);active=dict(s);scan={}
            _,active=self.m.scan_command('sweep_setup',dict(plan=self.plan,centers=[]),hw,active,g,self.expected,r,d,scan,c,rx)
            s['gain']='foreign'
            with self.assertRaisesRegex(RuntimeError,'SETTINGS_CHANGED'):
                self.m.scan_command('sweep_batch',dict(plan=self.plan,centers=[100100000]),hw,active,g,self.expected,r,d,scan,c,rx)
            self.assertIs(scan['local'],l);l.close.assert_not_called()

    def test_constructor_no_io_selects_narrow_entry_and_old_buffer_contract(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(host='192.0.2.1',fm=True)
        try:
            self.assertEqual(board.controller.entrypoint,'board_fm_scan_stream.py')
            self.assertEqual(board.options().frame_samples,32768)
            self.assertIsNone(board.controller.link)
        finally:board.close()

    def cancel_fixture(self, restore_ok):
        """Paused control-only substitute; forced cancellation must actually send finish and verify its recovery reply."""
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        from gnuradio.logicrf.managed import load_runtime
        board=make_grc_rx_device(host='192.0.2.1',fm=True)
        c=board.controller
        load_runtime(c.runtime,c.lock_path)
        c.paused=True
        c.info=dict(radio=dict(initial_calibrations='off',rx_center_frequency_hz={'1':98100000,'2':98100000}))
        radio=copy.deepcopy(c.info['radio'])
        c.link=SimpleNamespace(ready={'fixture':True},permitted=lambda:True,
            _command=Mock(side_effect=[{'guard':{'setup':True}},
                {'guard':dict(scan_restored=restore_ok,local_dma_closed=True,radio=radio)}]))
        return board,c

    def test_other_valid_fm_profiles_and_shared_tx_keep_cold_path(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board=make_grc_rx_device(host='192.0.2.1',fm=True)
        c=board.controller
        try:
            c.info=dict(parameters=dict(rx=[dict(channel=1,sample_rate_hz=1536000,rf_bandwidth_hz=500000)]),
                radio=dict(initial_calibrations='off',groups=[dict(channels=[1,2],editable=True,tx_shared=[])]))
            self.assertTrue(c.supports_fast_scan())
            c.info['parameters']['rx'][0]['rf_bandwidth_hz']=1000000
            self.assertFalse(c.supports_fast_scan())
            c.info['parameters']['rx'][0]['rf_bandwidth_hz']=500000
            c.info['radio']['groups'][0]['tx_shared']=[1]
            self.assertFalse(c.supports_fast_scan())
        finally:board.close()

    def test_cancel_after_setup_closes_local_dma_and_restores(self):
        board,c=self.cancel_fixture(True)
        cancel=Event();cancel.set()
        try:
            with TemporaryDirectory() as t:
                result=c.scan_stations(Path(t)/'scan',cancel,Mock())
            self.assertEqual(result['status'],'CANCELLED')
            self.assertTrue(result['scan_settings_restored'])
            self.assertEqual([call.args[0]['action'] for call in c.link._command.call_args_list],
                             ['sweep_setup','sweep_finish'])
        finally:
            c.link=None;c.finished=True;c.finish_errors=[];board.close()

    def test_failed_scan_restore_invalidates_receiver(self):
        board,c=self.cancel_fixture(False)
        cancel=Event();cancel.set()
        try:
            with TemporaryDirectory() as t,self.assertRaisesRegex(RuntimeError,'RESTORE_NOT_PROVED'):
                c.scan_stations(Path(t)/'scan',cancel,Mock())
            self.assertIn('RESTORE_NOT_PROVED',board.error())
        finally:
            c.link=None;c.finished=True;c.finish_errors=[]
            with self.assertRaisesRegex(RuntimeError,'RESTORE_NOT_PROVED'):board.close()


if __name__=='__main__':unittest.main()
# End of file: qa_fm_owner_scan.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
