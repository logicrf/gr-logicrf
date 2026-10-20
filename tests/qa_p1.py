"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_p1.py
@brief Hardware-free P1 construction, budget, failed-prepare cleanup and native diagnostic Sink checks.
@details Never start actual network IIO; control callbacks are substitutes; hardware qualification is separate.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.0
@date 2026-10-16
"""
import unittest
import weakref
from gnuradio import gr, blocks, logicrf


class P1Contracts(unittest.TestCase):
    """Cover new P1 lifecycle risks without collecting hardware data under fake ownership."""
    def test_construct_is_offline(self):
        """Construction must not trigger credentials, SSH, configuration or data buffers."""
        calls=[]
        board=logicrf.make_device(backend='iio',uri='ip:127.0.0.1',sample_rate=8e6,
                                 password_provider=lambda: calls.append('PASSWORD_FORBIDDEN'))
        source=logicrf.h102_rx_source(board,1)
        self.assertEqual(calls,[])
        self.assertEqual(board.options().backend,'iio')
        low=logicrf.make_device(backend='iio',uri='ip:127.0.0.1',sample_rate=192000,
                               password_provider=lambda: calls.append('PASSWORD_FORBIDDEN'))
        self.assertLessEqual(low.options().frame_samples/192000,.25)
        self.assertEqual(calls,[])
        low.close()
        self.assertIsNotNone(source)
        board.close()

    def test_budget_before_owner_callback(self):
        """Reject over-budget dual RX before prepare/providers."""
        calls=[]
        board=logicrf.make_device(backend='iio',uri='ip:127.0.0.1',sample_rate=20e6,
                                 password_provider=lambda: calls.append('UNEXPECTED'))
        graph=gr.top_block()
        for channel in (1,2):
            graph.connect(logicrf.h102_rx_source(board,channel),blocks.null_sink(gr.sizeof_gr_complex))
        try:
            graph.start();graph.wait()
            self.assertIn('BUDGET_EXCEEDED',board.error())
        finally:
            graph.stop();graph.wait()
        self.assertEqual(calls,[])
        with self.assertRaisesRegex(RuntimeError,'BUDGET_EXCEEDED'):board.close()

    def test_prepare_failure_finishes_once_without_context(self):
        """Mock prepare grants no permission; IIO rejects before context and calls finish; repeated close is idempotent."""
        options=logicrf.device_options();options.backend='iio';options.uri='ip:127.0.0.1'
        board=logicrf.device.make(options)
        reference=weakref.ref(board)
        calls=[]
        def prepare(mask):
            calls.append(('prepare',mask))
            actual=reference().options();actual.boot_id='offline-only';actual.metadata_json='{}'
            return actual
        board.set_session(prepare,lambda *args: self.fail('No buffer should be created'),lambda: calls.append(('finish',0)))
        graph=gr.top_block();graph.connect(logicrf.h102_rx_source(board,1),blocks.null_sink(gr.sizeof_gr_complex))
        try:
            graph.start();graph.wait()
            self.assertIn('PREPARED_PERMISSION_MISSING',board.error())
        finally:
            graph.stop();graph.wait();board.shutdown();board.set_session(None,None,None)
        self.assertEqual(calls,[('prepare',1),('finish',0)])

    def test_usb_construct_and_budget_are_offline(self):
        """USB construction needs no passwords; reject dual 61.44 M before either endpoint control."""
        calls=[]
        board=logicrf.make_device(backend='usb',uri='ip:127.0.0.1',sample_rate=61.44e6,
            password_provider=lambda:calls.append('BOARD'),host_password_provider=lambda:calls.append('HOST'))
        self.assertEqual(calls,[])
        self.assertEqual(board.options().queue_frames,512)
        graph=gr.top_block()
        for channel in (1,2):graph.connect(logicrf.h102_rx_source(board,channel),blocks.null_sink(gr.sizeof_gr_complex))
        graph.run()
        self.assertIn('USB_RX_BUDGET_EXCEEDED',board.error());self.assertEqual(calls,[])
        with self.assertRaisesRegex(RuntimeError,'USB_RX_BUDGET_EXCEEDED'):board.close()

    def test_native_probe_wrap_and_error_detection(self):
        """Native Sink checks across work and 65536 wrap; errors never silently pass."""
        values=[complex((n if n<32768 else n-65536)/32768,
                        (n if n<32768 else n-65536)/32768) for n in range(65536)]
        values+=values[:9]
        values[33]=values[32]
        graph=gr.top_block();sink=logicrf.stream_probe(True)
        graph.connect(blocks.vector_source_c(values,False),sink);graph.run()
        stats=sink.snapshot()
        self.assertEqual(stats['samples'],65545)
        self.assertEqual(stats['ramp_errors'],2)
        self.assertEqual(stats['iq_mismatch'],0)
        self.assertEqual(stats['nonfinite'],0)
        self.assertEqual(stats['out_of_range'],0)
        self.assertEqual(len(sink.prefix()),16)


if __name__=='__main__':unittest.main()

# End of file: qa_p1.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
