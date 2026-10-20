"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_fm_lifecycle.py
@brief Check FM cancellation scope, pre-pause ADC windows and hardware-free construction.
@details Mocks never qualify hardware; prevent active cancellation from bypassing ADC end and closing actors directly.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.1
@date 2026-10-17
"""
from threading import Event
import unittest
from gnuradio.logicrf.fm_managed import make_fm_device


class FmLifecycle(unittest.TestCase):
    def make_board(self):
        """Construction only, without GUI/network/RF."""
        return make_fm_device(stop_event=Event(), channel=1, uri='ip:192.0.2.1',
            configuration='1p536msps-0p5mhz', sample_rate=1536000,
            rx_frequency=96500000, tx_frequency=96500000)

    def test_cancel_scope_and_frame_size(self):
        board = self.make_board()
        control = board.controller
        self.assertEqual(board.options().frame_samples, 65536)
        control.stop_event.set()
        self.assertFalse(control.is_set())
        control.preparing = True
        self.assertTrue(control.is_set())
        with self.assertRaises(InterruptedError): control.check_cancelled()

    def test_pause_requires_closed_guard(self):
        board = self.make_board()
        control = board.controller
        control.hold_for_tune = True
        control.guard_records = {'rx1': {'arm': {}}}
        with self.assertRaisesRegex(RuntimeError, 'GUARD_NOT_CLOSED'):
            control.finish_direction('rx')
        self.assertFalse(control.paused)
        control.guard_records['rx1']['end'] = {'passed': True}
        control.finish_direction('rx')
        self.assertTrue(control.paused)
        self.assertFalse(control.recovery_confirmed)
        self.assertEqual(len(control.epochs), 1)

    def test_failed_post_cannot_become_no_owner_cancellation(self):
        from types import SimpleNamespace
        board = self.make_board()
        control = board.controller
        self.assertFalse(control.requires_recovery())
        control.session_link = SimpleNamespace(active=True)
        control.link = None
        self.assertTrue(control.requires_recovery())

    def test_tune_requires_live_paused_owner(self):
        board = self.make_board()
        with self.assertRaisesRegex(RuntimeError, 'PAUSED_RX_OWNER'):
            board.controller.retune(100000000)

if __name__ == '__main__': unittest.main()
# End of file: qa_fm_lifecycle.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
