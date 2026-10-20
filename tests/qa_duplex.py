"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_duplex.py
@brief Offline mixed-session construction, exclusive ownership, direction aggregation and request admission checks.
@details Explicit substitutes isolate SSH/hardware; separate board tests qualify actual GNU data/RF.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import ast
import math
from pathlib import Path
import unittest
from unittest.mock import patch
from gnuradio import logicrf
from gnuradio.logicrf.iio_tx_managed import IioTxController
from gnuradio.logicrf.managed import OwnerController


class DuplexTests(unittest.TestCase):
    """Separate lifecycle/hardware checks; missing graph/invalid RF parameters must never request credentials."""
    def board(self,**kwargs):
        kwargs.setdefault('sample_rate',8e6)
        return logicrf.make_duplex_device(uri='ip:127.0.0.1',
            password_provider=lambda:self.fail('credentials requested offline'),**kwargs)

    def test_offline_constructor_and_invalid_admission(self):
        board=self.board(rx_channels=(1,2),tx_channels=(2,),sample_rate=4e6);self.assertFalse(board.controller.rf_enabled)
        with self.assertRaisesRegex(ValueError,'NOT_PLANNED'):logicrf.h102_tx_sink(board,1)
        for kwargs in (dict(sample_rate=20e6,rx_channels=(1,2)),dict(sample_rate=8e6,rx_channels=(1,2),tx_channels=(1,2)),dict(rf_enabled='yes'),
                       dict(attenuation_db=10),dict(rx_gain_db={3:0}),dict(backend='usb',rx_channels=(1,2))):
            with self.assertRaises(ValueError):logicrf.make_duplex_device(uri='ip:127.0.0.1',**kwargs)
        board.close()

    def test_all_planned_blocks_required_before_owner(self):
        board=self.board();tx=logicrf.h102_tx_sink(board,1)
        with self.assertRaisesRegex(RuntimeError,'ALL_DECLARED'):board.controller.prepare_direction('tx',1)
        board.close();self.assertIsNotNone(tx)

    def test_one_prepare_last_finish_and_restart(self):
        board=self.board();c=board.controller
        rx=logicrf.h102_rx_source(board,1);tx=logicrf.h102_tx_sink(board,1)
        def prepared(controller,mask):
            self.assertEqual(mask,1);options=controller.native_ref().options()
            options.boot_id='offline';controller.info=dict(active='fake',parameters=dict(rx=[dict(channel=1,
                sample_rate_hz=8e6,rf_bandwidth_hz=4e6)]));return options
        with patch.object(IioTxController,'prepare',autospec=True,side_effect=prepared) as start,\
             patch.object(OwnerController,'finish',autospec=True) as finish:
            for repeat in range(2):
                c.prepare_direction('rx',1);c.prepare_direction('tx',1)
                self.assertEqual(start.call_count,repeat+1)
                c.finish_direction('tx');self.assertEqual(finish.call_count,repeat)
                c.finish_direction('rx');self.assertEqual(finish.call_count,repeat+1)
        self.assertIsNotNone(rx);self.assertIsNotNone(tx)

    def test_invalid_second_direction_does_not_change_first_plan(self):
        """Invalid RX frequency must also preserve the original TX profile/Fs/center-frequency plan."""
        board=self.board();before=board.tx_native.options()
        with self.assertRaises(ValueError):board.configure('4msps-2mhz-common',4e6,49e6,2.4e9)
        after=board.tx_native.options()
        self.assertEqual((before.configuration,before.sample_rate,before.center_frequency),
                         (after.configuration,after.sample_rate,after.center_frequency))
        board.close()

    def test_usb_cancel_keeps_rx_alive_until_tx_stop_ack(self):
        """Keep RX alive through TX guard/ACK so FT601 IN consumption cannot create artificial queue faults."""
        from types import SimpleNamespace
        from gnuradio.logicrf.iio_stream_managed import DuplexDevice
        calls=[]
        class Native:
            def __init__(self,direction):self.direction=direction
            def options(self):return SimpleNamespace(backend='usb')
            def invalidate(self,reason):calls.append(self.direction+'-invalidate')
            def shutdown(self):
                if self.direction=='tx':self_test.assertNotIn('rx-invalidate',calls)
                calls.append(self.direction+'-joined')
        self_test=self;board=DuplexDevice.__new__(DuplexDevice)
        board.tx_native=Native('tx');board.rx_native=Native('rx');board.native=board.tx_native
        board.controller=SimpleNamespace(prepared_options=object(),finish=lambda:calls.append('finish'))
        board.shutdown()
        self.assertTrue(board.controller.cancelling)
        self.assertEqual(calls,['tx-invalidate','tx-joined','rx-invalidate','rx-joined','finish'])

    def test_board_validates_continuous_rf_and_budgets_independently(self):
        # Execute hardware-free validate AST only; keep actor dependencies on-board without fabricating SDKs.
        path=Path(__file__).resolve().parents[1]/'python/logicrf/board_iio_stream.py'
        module=ast.parse(path.read_text());function=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='validate')
        namespace={'math':math};exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
        validate=namespace['validate'];base=dict(channels=[1,2],samples=0,rate=4e6,frequency=1_000_000_000,
            rx_channels=[1,2],rx_frequency=1_000_000_000,rf_enabled=False,rx_gain_db={},attenuation_db=40)
        self.assertEqual(validate(base),0)
        for change in (dict(rate=8e6),dict(samples=-1),dict(rf_enabled='yes'),dict(rx_frequency=49_000_000),
                       dict(attenuation_db=19),dict(rx_channels=[1,1]),dict(rx_gain_db={'1':float('nan')})):
            with self.assertRaises(ValueError):validate(dict(base,**change))


if __name__=='__main__':unittest.main()
# End of file: qa_duplex.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
