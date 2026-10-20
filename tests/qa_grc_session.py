"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_grc_session.py
@brief Check GRC control, warm restart, cancellation and native statistics with the actual GNU scheduler.
@details Bounded mocks/synthetic tags only, without RF/USB/hardware-time/board-recovery qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import time
import unittest
import json
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import pmt
from gnuradio import blocks, gr, logicrf
from gnuradio.logicrf.grc_session import GrcRxSession, make_grc_rx_device, observation
from gnuradio.logicrf.grc_recording import RecordingPlan, digest
from gnuradio.logicrf.grc_replay import ReplayPlan
from gnuradio.logicrf.logicrf_python import h102_replay_tagger


class GrcSessionTests(unittest.TestCase):
    """Actual scheduler without Qt; verify control/scheduler threads ended after every case."""
    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic()+timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('condition timeout')
            time.sleep(.01)

    def test_independent_rx_branches_preserve_unequal_eos(self):
        # GNU multi-input sync blocks stop peer upstream at shorter-input EOF; unsuitable for independent RX branches.
        counts = []
        for merged in (True, False):
            graph = gr.top_block()
            one = blocks.vector_source_c([1j]*1000, False)
            two = blocks.vector_source_c([1j]*1000000, False)
            observer = logicrf.stream_probe()
            graph.connect(two, observer)
            if merged:
                join = blocks.add_cc();sink = blocks.null_sink(gr.sizeof_gr_complex)
                graph.connect(one, (join, 0));graph.connect(two, (join, 1));graph.connect(join, sink)
            else:
                short = blocks.null_sink(gr.sizeof_gr_complex);long = blocks.null_sink(gr.sizeof_gr_complex)
                graph.connect(one, short);graph.connect(two, long)
            graph.run();counts.append(observer.snapshot()['samples'])
        self.assertLess(counts[0], 1000000)
        self.assertEqual(counts[1], 1000000)

    def test_native_statistics_and_time_quality(self):
        graph = gr.top_block()
        source = blocks.vector_source_c([complex(.25, -.5)]*4096, False)
        probe = logicrf.stream_probe()
        graph.connect(source, probe)
        graph.run()
        stats = probe.signal_snapshot()
        self.assertAlmostEqual(stats['mean_i'], .25)
        self.assertAlmostEqual(stats['mean_q'], -.5)
        self.assertAlmostEqual(stats['rms'], (.25**2+.5**2)**.5)
        self.assertEqual(stats['clipped_fraction'], 0)
        view = observation(probe, 'iio', 1536000)
        self.assertEqual(view['sample_index_kind'], 'host_output_not_adc')
        self.assertEqual(view['time_quality'], 'relative_only')
        self.assertFalse(view['utc_accuracy_verified'])

    def test_deferred_usb_post_is_after_native_eos_and_before_restart(self):
        """Actual mock scheduling verifies control ordering only, not FT601 hardware."""
        from types import SimpleNamespace
        native = make_grc_rx_device(backend='mock', sample_rate=100000)
        events = []

        class DeferredBoard:
            def __init__(self):
                self.controller = SimpleNamespace(defer_finish=True, recovery_confirmed=False,
                                                  info=None, run_output=None)
                self.rx_native = native
                self.tx_native = None

            def options(self):
                options = native.options();options.backend = 'usb'
                return options

            def error(self):return native.error()
            def ready(self):return native.ready()
            def stop_rx(self):native.request_rx_stop()
            def set_center_frequency(self, value):native.set_center_frequency(value)

            def close(self):
                if native.received(1):
                    stopped = [e for e in events if e['event'] == 'data_stopped']
                    if not stopped or not stopped[-1]['recovery_pending']:
                        raise AssertionError('POST_BEFORE_NATIVE_EOS')
                native.shutdown()
                self.controller.recovery_confirmed = True

        board = DeferredBoard()
        graph = gr.top_block()
        probe = logicrf.stream_probe()
        graph.connect(logicrf.h102_rx_source(native, 1), probe)
        session = GrcRxSession(graph, board, probes={1: probe}, notify=events.append)
        try:
            for generation in (1, 2):
                session.start()
                self.wait_for(lambda: session.state == 'running')
                self.assertEqual(probe.snapshot()['generation'], generation)
                session.pause()
                self.wait_for(lambda: session.state == 'idle')
                self.assertTrue(board.controller.recovery_confirmed)
            released = [e for e in events if e['event'] == 'owner_released']
            self.assertEqual(len(released), 2)
            self.assertTrue(all(e['owner_held'] for e in events if e['event'] == 'data_stopped'))
        finally:
            session.release(close=True)
            self.assertTrue(session.join(5))
        self.assertEqual(session.last_result['status'], 'PASS_SESSION')

    def test_start_pause_tune_restart_release(self):
        board = make_grc_rx_device(backend='mock', sample_rate=100000)
        graph = gr.top_block()
        source, probe = logicrf.h102_rx_source(board, 1), logicrf.stream_probe()
        graph.connect(source, probe)
        events = []
        session = GrcRxSession(graph, board, probes={1: probe}, notify=events.append)
        try:
            session.start()
            self.wait_for(lambda: session.state == 'running')
            self.assertTrue(probe.snapshot()['samples'])
            session.tune(100100000)
            self.wait_for(lambda: session.state == 'running' and session.epoch == 1)
            self.assertEqual(probe.snapshot()['generation'], 2)
            self.assertEqual(board.options().center_frequency, 100100000)
            self.assertFalse(session.tune(100100000))
            session.pause()
            self.wait_for(lambda: session.state == 'idle' and session.epoch == 2)
            session.start()
            self.wait_for(lambda: session.state == 'running')
            self.assertEqual(probe.snapshot()['generation'], 3)
            self.assertFalse(any(e.get('result', {}).get('status') == 'FAIL' for e in events))
        finally:
            session.release(close=True)
            self.assertTrue(session.join(5))
        self.assertEqual(session.state, 'closed', session.last_result)
        self.assertEqual(session.last_result['status'], 'PASS_SESSION')
        self.assertFalse(session.waiter.is_alive())

    def test_invalid_tune_does_not_interrupt(self):
        board = make_grc_rx_device(backend='mock', sample_rate=100000)
        graph = gr.top_block()
        source, probe = logicrf.h102_rx_source(board, 1), logicrf.stream_probe()
        graph.connect(source, probe)
        session = GrcRxSession(graph, board, probes={1: probe})
        try:
            session.start()
            self.wait_for(lambda: session.state == 'running')
            for frequency in (float('nan'), float('inf'), True, 49e6, 5801e6):
                with self.assertRaises(ValueError):
                    session.tune(frequency)
            self.assertEqual(session.state, 'running')
            self.assertEqual(session.epoch, 0)
        finally:
            session.release(close=True)
            self.assertTrue(session.join(5))

    def test_audio_callback_failure_still_stops_and_cannot_restart(self):
        board = make_grc_rx_device(backend='mock', sample_rate=100000)
        graph = gr.top_block()
        source, probe = logicrf.h102_rx_source(board, 1), logicrf.stream_probe()
        graph.connect(source, probe)

        def broken_callback():
            raise RuntimeError('injected_audio_callback_failure')

        session = GrcRxSession(graph, board, probes={1: probe}, before_stop=broken_callback)
        session.start()
        self.wait_for(lambda: session.state == 'running')
        session.pause()
        self.assertTrue(session.join(5))
        self.assertEqual(session.last_result['status'], 'FAIL')
        self.assertFalse(session.waiter.is_alive())
        session.release()
        with self.assertRaisesRegex(RuntimeError, 'NEW_FLOWGRAPH'):
            session.start()

    def test_unstarted_close_does_not_access_hardware(self):
        board = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='4msps-2mhz-common', sample_rate=4000000)
        graph = gr.top_block()
        session = GrcRxSession(graph, board)
        session.release(close=True)
        self.assertEqual(session.state, 'closed')
        self.assertIsNone(board.controller.run_output)
        self.assertEqual(board.controller.registered['rx'], set())

    def test_fm_protocol_is_not_used_for_general_dual_rx(self):
        board = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='4msps-2mhz-common', sample_rate=4000000)
        self.assertEqual(board.controller.entrypoint, 'board_rx_batch_stream.py')
        self.assertEqual(board.controller.frequency_range, (50000000, 5800000000))
        self.assertTrue(hasattr(board.controller, 'hold_for_tune'))
        with self.assertRaisesRegex(ValueError, 'FM_REQUIRES_SINGLE'):
            make_grc_rx_device(host='192.0.2.1', channels=(1, 2), fm=True)

    def test_strict_native_finite_recording_and_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/'recordings'
            board = make_grc_rx_device(backend='mock', sample_rate=100000, samples=12345)
            graph = gr.top_block()
            recording = RecordingPlan(board, root, (1, 2))
            probes = {}
            for channel in (1, 2):
                source = logicrf.h102_rx_source(board, channel)
                probes[channel] = logicrf.stream_probe()
                graph.connect(source, probes[channel])
                graph.connect(source, recording.sink(channel))
            self.assertFalse(root.exists())
            session = GrcRxSession(graph, board, probes=probes, recording=recording)
            manifests = []
            for iteration in range(2):
                session.start()
                self.assertTrue(session.join(6))
                self.assertEqual(session.state, 'released', session.last_result)
                self.assertEqual(recording.result['status'], 'PASS_MOCK_RECORDING')
                for channel in (1, 2):
                    item = recording.result['files'][str(channel)]
                    path = recording.directory/item['file']
                    self.assertEqual(item['samples'], 12345)
                    self.assertEqual(path.stat().st_size, 12345*8)
                    self.assertEqual(digest(path), item['sha256'])
                manifests.append(recording.directory)
            self.assertNotEqual(*manifests)
            replay = ReplayPlan(manifests[-1]/'recording.json', (1, 2))
            playback = gr.top_block()
            replay_observers = {}
            for channel in (1, 2):
                replay_observers[channel] = logicrf.stream_probe()
                playback.connect(replay.source(channel), replay.tagger(channel), replay_observers[channel])
            replay.prepare()
            playback.run()
            verified = replay.verify(observers=replay_observers)
            self.assertEqual(verified['samples'], {'1': 12345, '2': 12345})
            self.assertTrue(verified['metadata_restored'])
            replay.close()
            corrupt = manifests[-1]/'rx1.cf32'
            with corrupt.open('r+b') as stream:
                stream.write(b'broken!!')
            with self.assertRaisesRegex(ValueError, 'HASH_SIZE'):
                replay.prepare()

    def test_missing_record_branch_creates_no_partial(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/'recordings'
            board = make_grc_rx_device(backend='mock', sample_rate=100000)
            recording = RecordingPlan(board, root, (1, 2))
            recording.sink(1)
            with self.assertRaisesRegex(RuntimeError, 'ALL_CHANNELS'):
                recording.begin()
            self.assertFalse(root.exists())

    def test_interactive_fm_keeps_buffer_count_and_reduces_frame(self):
        board = make_grc_rx_device(host='192.0.2.1', fm=True)
        self.assertEqual(board.options().frame_samples, 32768)
        self.assertTrue(hasattr(board.controller, 'hold_for_tune'))
        graph = gr.top_block()
        session = GrcRxSession(graph, board)
        with self.assertRaisesRegex(ValueError, 'FM_FREQUENCY_RANGE'):
            session.tune(2400000000)

    def test_usb_replay_integer_tags_and_reject_invalid_time(self):
        with TemporaryDirectory() as directory:
            index = Path(directory)/'rx1.h1mi'
            rows = []
            for frame in range(2):
                row = bytearray(128)
                row[:8] = b'H1MI'+bytes([1, 1, 1, 0])
                for offset, size, value in ((8, 8, frame*2048), (16, 8, frame*2048),
                        (24, 8, 1 << 33), (32, 4, frame*4096), (36, 4, 3072000),
                        (40, 4, frame), (44, 4, 7), (48, 4, 2048), (52, 4, 142),
                        (56, 8, (1 << 60)+17), (64, 8, 0x123456789ABC), (72, 8, 1536000),
                        (80, 4, 1), (84, 4, 500000), (88, 8, 98100000), (96, 4, 20000)):
                    row[offset:offset+size] = value.to_bytes(size, 'little')
                rows.append(row)
            index.write_bytes(b''.join(rows))
            tagger = h102_replay_tagger(1)
            tagger.configure(str(index), 'usb', 1536000, 98100000, '1p536msps-0p5mhz')
            graph = gr.top_block()
            probe = logicrf.stream_probe()
            graph.connect(blocks.vector_source_c([complex(.25, -.5)]*4096, False), tagger, probe)
            graph.run()
            self.assertEqual(tagger.error(), '')
            self.assertEqual(probe.snapshot().get('usb_tag_errors', 0), 0)
            view = observation(probe, 'usb', 1536000)
            self.assertEqual(view['hardware_time']['stream_id'], (1 << 60)+17)
            self.assertEqual(view['hardware_time']['utc_anchor_seconds'], 1 << 33)
            self.assertEqual(view['hardware_time']['first_sample_index'], 2048)
            self.assertEqual(view['time_quality'], 'device_digital_valid_utc_uncalibrated')
            self.assertFalse(view['utc_accuracy_verified'])
            rows[0][6] = 0
            index.write_bytes(b''.join(rows))
            graph.run()
            self.assertIn('INDEX_CONTRACT', tagger.error())


if __name__ == '__main__':
    unittest.main()
# End of file: qa_grc_session.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
