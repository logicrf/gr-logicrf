"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_tx_flowgraph.py
@brief Verify finite TX, continuous upstream, errors and stop in the actual GNU scheduler.
@details C++ converts/hashes; Python supplies small independent reference values and graph control.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.4.0
@date 2026-10-16
"""
import time
import unittest
import numpy as np
import pmt
from gnuradio import blocks, gr, logicrf


def oracle_signature(words):
    """Compute independent 64-bit rotate-xor signatures with low-I/high-Q wire ordering."""
    value = 0
    for word in words:
        value = (((value << 7) | (value >> 57)) ^ word) & ((1 << 64) - 1)
    return value


class TxFlowgraphTests(unittest.TestCase):
    """P3a simulated scheduling only, not hardware TX qualification."""
    def test_dual_finite_and_restart(self):
        """Require exact partial finite tails, channel identity and same-graph restart."""
        count = 12345
        board = logicrf.make_device(direction='tx', tx_samples=count, sample_rate=192000,
                                    queue_frames=2)
        graph = gr.top_block()
        expected = {}
        sources = []
        for channel in (1, 2):
            words, iq = [], []
            for n in range(count):
                i, q = ((n * 7 + channel * 97) % 65536) - 32768, (n % 65536) - 32768
                words.append((i & 65535) | ((q & 65535) << 16))
                iq.append(complex(i / 32768, q / 32768))
            expected[channel] = oracle_signature(words)
            source = blocks.vector_source_c(iq, False)
            sources.append(source)
            graph.connect(source, logicrf.h102_tx_sink(board, channel))
        for run in range(2):
            # vector_source does not rewind at start; explicitly reset finite sources before restarting the same Sink.
            for source in sources:
                source.rewind()
            graph.run()
            self.assertEqual(board.error(), '', f'run={run}; stats={[board.tx_stats(c) for c in (1, 2)]}')
            for channel in (1, 2):
                stats = board.tx_stats(channel)
                self.assertEqual(stats['accepted_samples'], count)
                self.assertEqual(stats['mock_consumed_samples'], count)
                self.assertEqual(stats['signature'], expected[channel])
                self.assertEqual(stats['discarded_samples'], 0)
                self.assertEqual(stats['finite_complete'], 1)

    def test_continuous_eof_empty_restart_and_graceful_stop(self):
        """Preserve actual counts for empty streams, partial EOF, restart and dual-channel requested sealing."""
        for count in (0,37,9001):
            board=logicrf.make_device(direction='tx',tx_samples=0,sample_rate=192000)
            source=blocks.vector_source_c([.25j]*count,False);graph=gr.top_block()
            graph.connect(source,logicrf.h102_tx_sink(board,1))
            for repeat in range(2):
                source.rewind();graph.run();self.assertEqual(board.error(),'')
                self.assertEqual(board.tx_stats(1)['mock_consumed_samples'],count)
                self.assertEqual(board.tx_stats(1)['finite_complete'],1)
        board=logicrf.make_device(direction='tx',tx_samples=0,sample_rate=192000)
        graph=gr.top_block()
        for ch in (1,2):graph.connect(blocks.vector_source_c([.25j],True),logicrf.h102_tx_sink(board,ch))
        graph.start()
        try:
            time.sleep(.03);target=board.request_tx_stop();graph.wait()
            self.assertEqual(board.error(),'')
            for ch in (1,2):self.assertEqual(board.tx_stats(ch)['mock_consumed_samples'],target)
        finally:board.shutdown();graph.stop();graph.wait()

    def test_continuous_external_stop_is_cancelled(self):
        """Forced external stop must never masquerade as natural upstream EOF."""
        board=logicrf.make_device(direction='tx',tx_samples=0,sample_rate=192000)
        graph=gr.top_block();graph.connect(blocks.vector_source_c([0j],True),logicrf.h102_tx_sink(board,1))
        graph.start();time.sleep(.03);graph.stop();graph.wait()
        self.assertIn('CANCELLED',board.error());self.assertEqual(board.tx_stats(1)['finite_complete'],0)

    def test_repeating_upstream_stops_at_finite_limit(self):
        """Continuous upstream consumes the declared length only, without implicit unlimited TX or final-frame replay."""
        board = logicrf.make_device(direction='tx', tx_samples=9001, sample_rate=192000)
        graph = gr.top_block()
        graph.connect(blocks.vector_source_c([.25-.5j], True), logicrf.h102_tx_sink(board, 2))
        graph.run()
        self.assertEqual(board.error(), '')
        stats = board.tx_stats(2)
        self.assertEqual(stats['accepted_samples'], 9001)
        self.assertEqual(stats['mock_consumed_samples'], 9001)
        self.assertEqual(stats['signature'], oracle_signature([0xc0002000] * 9001))

    def test_short_input_and_invalid_iq_are_not_success(self):
        """Early upstream EOF/invalid IQ must not pass as complete streams."""
        for values, reason in (([0j] * 100, 'CANCELLED'), ([0j, complex(float('nan'), 0)], 'INVALID_IQ')):
            board = logicrf.make_device(direction='tx', tx_samples=12345)
            graph = gr.top_block()
            graph.connect(blocks.vector_source_c(values, False), logicrf.h102_tx_sink(board, 1))
            graph.run()
            self.assertIn(reason, board.error())
            self.assertEqual(board.tx_stats(1)['finite_complete'], 0)

    def test_active_stop_and_direction_guards(self):
        """Bound cancellation and retain tail counts; reject direction conflicts at construction."""
        board = logicrf.make_device(direction='tx', tx_samples=100000000, sample_rate=192000,
                                    queue_frames=2)
        with self.assertRaisesRegex(ValueError, 'RX_SOURCE_REQUIRES'):
            logicrf.h102_rx_source(board, 1)
        graph = gr.top_block()
        graph.connect(blocks.vector_source_c([0j], True), logicrf.h102_tx_sink(board, 1))
        graph.start()
        time.sleep(.05)
        with self.assertRaisesRegex(RuntimeError, 'STOP_REQUIRED'):
            board.configure('mock', 192000, 200e6)
        start = time.monotonic()
        graph.stop()
        graph.wait()
        self.assertLess(time.monotonic() - start, .5)
        stats = board.tx_stats(1)
        self.assertIn('CANCELLED', board.error())
        self.assertEqual(stats['accepted_samples'], stats['mock_consumed_samples'] + stats['discarded_samples'])
        self.assertEqual(stats['finite_complete'], 0)
        with self.assertRaisesRegex(ValueError, 'explicit ip:host URI'):
            logicrf.make_device(backend='iio', direction='tx')
        arbitrary=logicrf.make_device(backend='usb', direction='tx',uri='ip:127.0.0.1')
        self.assertEqual(arbitrary.options().tx_validation,'none')
        arbitrary.close()

    def test_starvation_event_without_more_input(self):
        """When upstream stays connected but stops supplying IQ, errors must not await the next Sink.work."""
        class PausedSource(gr.sync_block):
            """Test-only substitute pauses after prefill to simulate interrupted external supply."""
            def __init__(self):
                gr.sync_block.__init__(self, name='paused_test_source', in_sig=None, out_sig=[np.complex64])
                self.remaining = 8192

            def work(self, inputs, outputs):
                """Emit finite prefill, then remain connected and return zero."""
                count = min(len(outputs[0]), self.remaining)
                outputs[0][:count] = 0j
                self.remaining -= count
                return count

        board = logicrf.make_device(direction='tx', tx_samples=100000, sample_rate=192000)
        graph = gr.top_block()
        sink = logicrf.h102_tx_sink(board, 1)
        debug = blocks.message_debug()
        # Retain strong Python references to custom blocks; C++ graph adapters do not own these instances.
        source = PausedSource()
        graph.connect(source, sink)
        graph.msg_connect((sink, 'events'), (debug, 'store'))
        graph.start()
        try:
            until = time.monotonic() + 2
            while not debug.num_messages() and time.monotonic() < until:
                time.sleep(.01)
            self.assertGreater(debug.num_messages(), 0)
            event = pmt.to_python(debug.get_message(0))
            self.assertEqual(event['stage'], 'fault')
            self.assertIn('STARVED', event['error'])
        finally:
            board.shutdown()
            graph.stop()
            graph.wait()


if __name__ == '__main__':
    unittest.main()

# End of file: qa_tx_flowgraph.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
