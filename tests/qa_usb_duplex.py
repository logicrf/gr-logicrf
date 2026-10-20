"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_duplex.py
@brief Offline checks for four 1RX1TX combinations, exclusive ownership and READY ordering.
@details Explicit preparation substitutes never open FT/access boards; actual data qualification is separate.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gnuradio import logicrf
from gnuradio.logicrf.usb_stream_managed import UsbStreamTxController
from qa_usb_flowgraph import configuration


class UsbDuplexTests(unittest.TestCase):
    """Construct without credentials; verify all native masks/combined budgets before device operations."""
    def board(self,rx=1,tx=1):
        return logicrf.make_duplex_device(backend='usb',uri='ip:127.0.0.1',sample_rate=8e6,
            rx_channels=(rx,),tx_channels=(tx,),rx_frequency=2.4e9,tx_frequency=2.45e9,
            tx_samples=0,usb_seconds=0,password_provider=lambda:self.fail('unexpected board access'),
            host_password_provider=lambda:self.fail('unexpected host access'))

    def test_four_masks_and_invalid_admission(self):
        for rx in (1,2):
            for tx in (1,2):
                board=self.board(rx,tx);self.assertEqual(board.controller.masks(0,True),(1<<(rx-1),1<<(tx-1)))
                self.assertEqual(board.options().tx_validation,'none');board.close()
        for kwargs in (dict(rx_channels=(1,2)),dict(tx_channels=(1,2)),dict(sample_rate=61.44e6),
                       dict(rf_enabled='yes'),dict(rx_gain_db={1:31}),dict(sample_limit=2048)):
            with self.assertRaises(ValueError):logicrf.make_duplex_device(backend='usb',uri='ip:127.0.0.1',**kwargs)

    def test_single_direction_radio_plans_without_io(self):
        for rx,tx in (((1,2),()),((),(1,2))):
            board=logicrf.make_duplex_device(backend='usb',uri='ip:127.0.0.1',sample_rate=8e6,
                rx_channels=rx,tx_channels=tx,rx_frequency=1e9,tx_frequency=1e9,tx_samples=0,usb_seconds=0,
                rf_enabled=bool(tx),password_provider=lambda:self.fail('unexpected board access'))
            controller=board.controller;controller.info={'active':'8msps-4mhz-common'}
            if rx:board.set_rx_gain(1,15)
            if tx:board.set_tx_attenuation(35)
            plan=controller.radio_plan()
            self.assertEqual(plan['rx_channels'],list(rx));self.assertEqual(plan['tx_channels'],list(tx))
            self.assertEqual(plan['rf_enabled'],bool(tx));board.close()

    def test_shared_prepare_and_receiver_ready_precedes_arm(self):
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        with tempfile.TemporaryDirectory(prefix='duplex-offline-',dir=project/'tmp') as root:
            board=self.board(2,1);controller=board.controller
            rx=logicrf.h102_rx_source(board,2);tx=logicrf.h102_tx_sink(board,1)
            def prepare(owner,mask):
                self.assertEqual(mask,1);options=board.tx_native.options();options.boot_id='offline';options.metadata_json='{}'
                owner.run_output=Path(root);owner.ready=dict(rx_configuration_hex=configuration().hex())
                owner.expected=dict(rx_sha256=hashlib.sha256(configuration()).hexdigest(),rx_stream_id='43',configuration_id=29)
                owner.info=dict(radio=dict(rx_center_frequency_hz={'2':2_400_000_000}),parameters=dict(rx=[dict(channel=2,sample_rate_hz=8e6,rf_bandwidth_hz=4e6)]))
                return options
            with patch.object(UsbStreamTxController,'prepare',autospec=True,side_effect=prepare) as start,\
                 patch.object(UsbStreamTxController,'finish',autospec=True) as finish:
                actual_rx=controller.prepare_direction('rx',2);controller.prepare_direction('tx',1)
                self.assertEqual(start.call_count,1);self.assertEqual(actual_rx.usb_stream_id,43)
                self.assertEqual(actual_rx.usb_socket,controller.rx_socket)
                controller.phase='queue';controller.receiver_ready=False
                with patch.object(UsbStreamTxController,'_step',autospec=True) as step:
                    controller._step({},{});step.assert_not_called()
                    controller.receiver_ready=True;controller._step({},{});self.assertEqual(step.call_count,1)
                controller.finish_direction('rx');finish.assert_not_called()
                controller.finish_direction('tx');self.assertEqual(finish.call_count,1)
            self.assertIsNotNone(rx);self.assertIsNotNone(tx)


if __name__=='__main__':unittest.main()
# End of file: qa_usb_duplex.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
