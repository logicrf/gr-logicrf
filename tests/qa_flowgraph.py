"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_flowgraph.py
@brief Check P0 dual-channel data/tags/stop/restart with the real GNU Radio scheduler.
@details Finite/continuous mocks never connect hardware; no extra Throttle masks hardware throughput.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-16
"""
import time
import unittest
import pmt
from gnuradio import blocks, gr, logicrf


class FlowgraphTests(unittest.TestCase):
    """Verify P0 scheduler contracts only, never treat mock samples as hardware traffic."""
    def test_dual_channel_exact_tail_and_tags(self):
        """Require exact partial final frames, IQ16 normalization and tag offsets."""
        board = logicrf.make_device(mock_samples=12345, sample_rate=100000)
        graph = gr.top_block()
        sinks = []
        for channel in (1, 2):
            source = logicrf.h102_rx_source(board, channel)
            sink = blocks.vector_sink_c()
            graph.connect(source, sink)
            sinks.append(sink)
        graph.run()
        self.assertEqual(board.error(), '')
        for channel, sink in enumerate(sinks, 1):
            data = sink.data()
            self.assertEqual(len(data), 12345)
            code = (channel - 1) * 97
            self.assertEqual(data[0], complex((code - 32768) / 32768, (32767 - code) / 32768))
            tags = {pmt.symbol_to_string(t.key): t for t in sink.tags()}
            self.assertEqual(set(tags), {'rx_rate', 'rx_freq', 'h102_config'})
            self.assertTrue(all(tag.offset == 0 for tag in tags.values()))
            meta = pmt.to_python(tags['h102_config'].value)
            self.assertEqual(meta['channel'], channel)
            self.assertEqual(meta['backend'], 'mock')
            self.assertEqual(pmt.to_double(tags['rx_rate'].value), 100000)

    def test_stop_restart_and_tune_gate(self):
        """Bound continuous stop; static tuning takes effect on the next run."""
        board = logicrf.make_device(sample_rate=100000)
        source = logicrf.h102_rx_source(board, 1)
        graph = gr.top_block()
        graph.connect(source, blocks.null_sink(gr.sizeof_gr_complex))
        for frequency in (150e6, 200e6):
            board.set_center_frequency(frequency)
            graph.start()
            with self.assertRaisesRegex(RuntimeError, 'STOP_REQUIRED'):
                board.set_center_frequency(300e6)
            time.sleep(.04)
            start = time.monotonic()
            graph.stop(); graph.wait()
            self.assertLess(time.monotonic() - start, .5)
            self.assertEqual(board.error(), '')
            self.assertGreater(board.produced(), 0)

    def test_command_status_and_active_tune_rejection(self):
        """Control replies asynchronously; reject active tuning explicitly without fabricating metadata changes."""
        board = logicrf.make_device(sample_rate=100000)
        source = logicrf.h102_rx_source(board, 1)
        graph = gr.top_block()
        graph.connect(source, blocks.null_sink(gr.sizeof_gr_complex))
        debug = blocks.message_debug()
        graph.msg_connect((source, 'events'), (debug, 'store'))
        graph.start()
        try:
            for request in ({'action': 'status'}, {'action': 'tune', 'frequency_hz': 200000000}):
                source.to_basic_block()._post(pmt.intern('command'), pmt.to_pmt(request))
            until = time.monotonic() + 1
            while debug.num_messages() < 2 and time.monotonic() < until:
                time.sleep(.005)
            self.assertEqual(debug.num_messages(), 2)
            replies = [pmt.to_python(debug.get_message(i)) for i in range(2)]
            self.assertEqual(replies[0]['status'], 'ok')
            self.assertEqual(replies[0]['rx_freq'], 150e6)
            self.assertEqual(replies[1]['status'], 'error')
            self.assertIn('STOP_REQUIRED', replies[1]['error'])
            self.assertEqual(board.options().center_frequency, 150e6)
        finally:
            graph.stop(); graph.wait()

    def test_duplicate_channel_and_invalid_options(self):
        """Reject conflicting/invalid input before hardware access."""
        board = logicrf.make_device()
        source = logicrf.h102_rx_source(board, 1)
        with self.assertRaisesRegex(RuntimeError, 'BUSY'):
            logicrf.h102_rx_source(board, 1)
        with self.assertRaises(ValueError):
            logicrf.make_device(sample_rate=float('nan'))
        self.assertIsNotNone(source)


if __name__ == '__main__':
    unittest.main()

# End of file: qa_flowgraph.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
