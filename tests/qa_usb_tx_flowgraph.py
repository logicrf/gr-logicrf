"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_tx_flowgraph.py
@brief Test actual USB TX C++ path, GNU scheduling and control checks with local socket fixtures.
@details Mock ownership never connects boards; check frame payloads/start/END/channel/counts and permit cancellation.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.5.0
@date 2026-10-16
@note Local sockets/mock control never qualify FT601/SSI/RF.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
from threading import Event, Thread
import time
import unittest
from gnuradio import blocks, gr, logicrf


def templates(rate, bandwidth, frequency):
    """Independently create frozen TX templates with zero immediate-transmission time fields."""
    data = bytearray(256)
    for ch in (1, 2):
        h = memoryview(data)[(ch-1)*128:ch*128]
        h[:4] = b'H102'
        h[4:9] = bytes((3, 2, ch, 1, 128))
        for offset, fmt, value in ((16, 'I', 8192), (32, 'Q', 43), (40, 'I', 29),
                (48, 'Q', rate), (56, 'I', 1), (60, 'I', 2048), (64, 'Q', frequency),
                (72, 'I', bandwidth), (96, 'Q', 0x123456789abc)):
            struct.pack_into('<'+fmt, h, offset, value)
        h[104:110] = b'\1'*6
    return data


class LocalOwner:
    """Local mock control/socket consumers only; retain all thread references and clean up explicitly."""
    def __init__(self, board, root, stall=False, play_delay=None):
        self.board, self.root, self.stall = board, root, stall
        self.stopped, self.received = Event(), Event()
        self.calls, self.wire, self.failure = [], bytearray(), None
        self.thread = None
        self.play_delay, self.control_thread = play_delay, None
        self.prefilled, self.play_confirmed = Event(), Event()
        self.bytes_before_play = 0

    def prepare(self, mask):
        """Return explicit fake board identity/actual private socket paths without hardware API calls."""
        options = self.board.options()
        options.boot_id, options.metadata_json = 'offline-test-only', '{"hardware":false}'
        options.rf_bandwidth, options.usb_stream_id, options.usb_time_epoch = 4_000_000, 43, 29
        options.usb_tx_seed = 0x76543210
        options.usb_socket = str(self.root/'wire.sock')
        options.usb_tx_templates = str(self.root/'templates.bin')
        Path(options.usb_tx_templates).write_bytes(templates(int(options.sample_rate), 4_000_000,
                                                          int(options.center_frequency)))
        self.board.renew_lease()
        self.thread = Thread(target=self.consume, args=(options.usb_socket,), daemon=True)
        self.thread.start()
        if self.play_delay is not None:
            self.control_thread = Thread(target=self.delayed_play, daemon=True)
            self.control_thread.start()
        return options

    def delayed_play(self):
        """Delay control acknowledgement and check data supply continues throughout."""
        while not self.prefilled.wait(.01):
            if self.stopped.is_set():
                return
        if self.stopped.wait(self.play_delay):
            return
        self.bytes_before_play = len(self.wire)
        self.board.confirm_tx_playing()
        self.play_confirmed.set()

    def consume(self, path):
        """Read all actual C++ wire bytes or pause to test production Sink paths with non-Python IQ generation."""
        try:
            with socket.socket(socket.AF_UNIX) as stream:
                until = time.monotonic()+3
                while True:
                    if self.stopped.is_set():
                        return
                    try:
                        stream.connect(path)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        if time.monotonic()>until:
                            raise
                        time.sleep(.005)
                if self.stall:
                    self.stopped.wait(3)
                    return
                while True:
                    data = stream.recv(7001)
                    if not data:
                        break
                    self.wire.extend(data)
                    if len(self.wire)>=2*8*8320:
                        self.prefilled.set()
        except Exception as error:
            self.failure = repr(error)
        finally:
            self.received.set()

    def guard(self, channel, stage):
        """Model low-rate boundaries only; never name simulated finish as actual SSI success."""
        self.calls.append(stage)
        if stage=='usb-tx-play' and self.play_delay is not None and not self.play_confirmed.wait(1):
            raise RuntimeError('DATA_PATH_BLOCKED_ON_PLAY_CONTROL')
        if stage=='usb-tx-finish' and not self.received.wait(2):
            raise RuntimeError('LOCAL_TEST_EOF_TIMEOUT')
        if stage=='usb-tx-finish' and self.play_delay is not None and not self.play_confirmed.wait(2):
            raise RuntimeError('LOCAL_TEST_PLAY_TIMEOUT')
        if stage=='usb-tx-stop':
            self.stopped.set()

    def finish(self):
        """Release local fixtures after data workers finish without system ownership/USB quotas."""
        self.stopped.set()
        if self.thread:
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError('LOCAL_TEST_THREAD_NOT_JOINED')
        if self.control_thread:
            self.control_thread.join(2)


class UsbTxFlowgraphTests(unittest.TestCase):
    """Actual C++ data/GNU scheduler with explicitly local mock control/data endpoints."""
    def setUp(self):
        parent = Path(os.environ['LOGICRF_PROJECT_ROOT'])/'tmp'
        parent.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='tx-', dir=parent)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def device(self, samples=32768):
        """Construct USB digital Devices requiring explicit managed callbacks, without network effects."""
        options = logicrf.device_options()
        options.direction, options.backend, options.tx_validation = 'tx', 'usb', 'ramp32'
        options.tx_samples, options.frame_samples, options.queue_frames = samples, 2048, 2
        options.sample_rate, options.center_frequency = 8_000_000, 2_450_000_000
        return logicrf.device.make(options)

    def test_arbitrary_continuous_eof_and_explicit_stop(self):
        """Local ownership checks arbitrary IQ/natural EOF/future shared END without hardware qualification."""
        for natural in (True,False):
            board=self.device(0);options=board.options();options.tx_validation='none'
            board=logicrf.device.make(options)
            owner=LocalOwner(board,self.root);board.set_session(owner.prepare,owner.guard,owner.finish)
            graph=gr.top_block()
            for ch in (1,2):
                graph.connect(blocks.vector_source_c([.125-.25j]*16384,not natural),logicrf.h102_tx_sink(board,ch))
            graph.start()
            try:
                if natural:target=16384
                else:
                    self.assertTrue(owner.prefilled.wait(2));target=board.request_tx_stop()
                graph.wait();self.assertEqual(board.error(),'')
                self.assertEqual(len(owner.wire),target//2048*8320*2)
                counts={1:0,2:0};ends={1:0,2:0}
                for offset in range(0,len(owner.wire),8320):
                    frame=owner.wire[offset:offset+8320];ch=frame[6]
                    self.assertEqual(frame[128:],struct.pack('<hh',4096,-8192)*2048)
                    self.assertEqual(struct.unpack_from('<Q',frame,24)[0],counts[ch]*2048)
                    if frame[11]:self.assertEqual(counts[ch]+1,target//2048)
                    ends[ch]+=frame[11];counts[ch]+=1
                self.assertEqual(ends,{1:1,2:1})
                for ch in (1,2):self.assertEqual(board.tx_stats(ch)['accepted_samples'],target)
            finally:board.shutdown();graph.stop();graph.wait()

    def test_dual_input_wire_and_end(self):
        """Verify both channels' IQ/END through actual GNU Source-Sink-socket, not historical CLI streams."""
        board = self.device()
        owner = LocalOwner(board, self.root, play_delay=.2)
        board.set_session(owner.prepare, owner.guard, owner.finish)
        graph = gr.top_block()
        for ch in (1, 2):
            graph.connect(logicrf.tx_ramp_source(board, ch), logicrf.h102_tx_sink(board, ch))
        graph.run()
        self.assertEqual(board.error(), '')
        self.assertIsNone(owner.failure)
        self.assertEqual(owner.calls, ['usb-tx-finish'])
        self.assertGreater(owner.bytes_before_play, 2*8*8320)
        self.assertEqual(len(owner.wire), 32768//2048*8320*2)
        indices = {1: 0, 2: 0}
        for offset in range(0, len(owner.wire), 8320):
            frame = memoryview(owner.wire)[offset:offset+8320]
            channel = frame[6]
            sequence = indices[channel]
            self.assertEqual(frame[:6].tobytes(), b'H102\3\2')
            self.assertEqual((frame[10], frame[11]), (sequence==0, sequence==15))
            self.assertEqual(struct.unpack_from('<IQ', frame, 20), (sequence, sequence*2048))
            words = struct.unpack_from('<2048I', frame, 128)
            seed = 0x76543210 ^ (0x6d2b79f5 if channel==2 else 0)
            expected = tuple(((seed+(sequence*2048+n)*0x9e3779b9)&0xffffffff)^0xc0000000
                             for n in range(2048))
            self.assertEqual(words, expected)
            indices[channel] += 1
        self.assertEqual(indices, {1: 16, 2: 16})

    def test_wrong_pattern_rejected_before_any_payload(self):
        """Arbitrary input cannot silently bypass digital ownership; invalid segments never publish."""
        board = self.device()
        owner = LocalOwner(board, self.root)
        board.set_session(owner.prepare, owner.guard, owner.finish)
        graph = gr.top_block()
        graph.connect(blocks.vector_source_c([0j]*2048, False), logicrf.h102_tx_sink(board, 1))
        graph.run()
        self.assertIn('RAMP32_INPUT_MISMATCH', board.error())
        self.assertEqual(board.tx_stats(1)['accepted_samples'], 0)
        self.assertEqual(len(owner.wire), 0)

    def test_cancel_socket_backpressure(self):
        """Bound immediate cancellation of paused consumers without socket/ownership/GIL deadlocks."""
        board = self.device(1_048_576)
        owner = LocalOwner(board, self.root, stall=True)
        board.set_session(owner.prepare, owner.guard, owner.finish)
        graph = gr.top_block()
        graph.connect(logicrf.tx_ramp_source(board, 1), logicrf.h102_tx_sink(board, 1))
        graph.start()
        time.sleep(.1)
        start = time.monotonic()
        board.shutdown()
        graph.stop()
        graph.wait()
        self.assertLess(time.monotonic()-start, 2)
        self.assertIn('CANCELLED', board.error())
        self.assertIn('usb-tx-stop', owner.calls)

    def test_cancel_does_not_hide_independent_post_failure(self):
        """After actual C++ socket cancellation, injected post failures remain visible alongside first CANCELLED."""
        board=self.device(1_048_576);owner=LocalOwner(board,self.root,stall=True)
        def finish():
            owner.finish()
            raise RuntimeError('INJECTED_POST_FAILURE')
        board.set_session(owner.prepare,owner.guard,finish)
        graph=gr.top_block();graph.connect(logicrf.tx_ramp_source(board,1),logicrf.h102_tx_sink(board,1))
        graph.start();time.sleep(.1);board.shutdown();graph.stop();graph.wait()
        self.assertIn('CANCELLED',board.error());self.assertIn('TX_OWNER_POST:',board.error())
        self.assertIn('INJECTED_POST_FAILURE',board.error())

    def test_host_receipts_allow_concurrent_heartbeat_writers(self):
        """Concurrent actual atomic-file transactions must neither collide on fixed .tmp nor leave partial JSON."""
        from concurrent.futures import ThreadPoolExecutor
        script=Path(__file__).resolve().parents[1]/'scripts/usb_host_guard.py'
        spec=importlib.util.spec_from_file_location('host_receipt_qa',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        path=self.root/'heartbeat.json'
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda n:module.save(path,{'n':n}),range(128)))
        self.assertIn(json.loads(path.read_text())['n'],range(128))
        self.assertEqual(list(self.root.glob('heartbeat.json.tmp*')),[])

    def test_host_control_requires_exact_phase_and_quiet_proof(self):
        """Production host guard handshake checks never replace actual drain proofs with fake ACKs."""
        script = Path(__file__).resolve().parents[1]/'scripts/usb_host_guard.py'
        spec = importlib.util.spec_from_file_location('test_usb_host_guard', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = dict(drain_requested=True, flushed=False, ready=False, stop_requested=False)
        proof = dict(ok=True, rx_control=0, tx_control=0x20000000, state='CONFIGURED',
                     tx=dict(state='DRAINING', snapshot=dict(decoded=dict(usb_flags=0x480))))
        self.assertEqual(module.tx_control_byte({'mask': 3}, state, dict(op='drain_idle', proof=proof)), b'A')
        for flags in (0, 0x80, 0x400, 0x481):
            bad = json.loads(json.dumps(proof))
            bad['tx']['snapshot']['decoded']['usb_flags'] = flags
            with self.assertRaisesRegex(RuntimeError, 'DRAIN_PROOF'):
                module.tx_control_byte({'mask': 3}, state, dict(op='drain_idle', proof=bad))
        with self.assertRaisesRegex(RuntimeError, 'PLAY_PROOF'):
            module.tx_control_byte({'mask': 3}, state, dict(op='play', proof=proof))
        flushed = dict(state, flushed=True)
        configured = dict(ok=True, rx_control=0, tx_control=0, state='CONFIGURED')
        self.assertEqual(module.tx_control_byte({'mask': 3}, flushed,
                         dict(op='drain_finished', proof=configured)), b'Q')
        playing = dict(ok=True, rx_control=0, tx_control=0xc0000003, state='RUNNING')
        ready = dict(flushed, ready=True)
        self.assertEqual(module.tx_control_byte({'mask': 3}, ready, dict(op='play', proof=playing)), b'S')
        with self.assertRaisesRegex(RuntimeError, 'PLAY_PROOF'):
            module.tx_control_byte({'mask': 1}, ready, dict(op='play', proof=playing))
        stopped = dict(proof, state='STOPPED')
        self.assertEqual(module.tx_control_byte({'mask': 3}, dict(ready, stop_requested=True),
                         dict(op='ack', proof=stopped)), b'\n')
        with self.assertRaisesRegex(RuntimeError, 'DRAIN_PROOF'):
            module.tx_control_byte({'mask': 3}, dict(ready, stop_requested=True),
                                   dict(op='ack', proof=dict(stopped, tx_control=0)))
        mixed=dict(direction='duplex',mask=1,rx_mask=2)
        live=dict(ok=True,state='RUNNING',rx_control=0x80000002,tx_control=0xc0000001)
        self.assertEqual(module.tx_control_byte(mixed,ready,dict(op='play',proof=live)),b'S')
        with self.assertRaisesRegex(RuntimeError,'USB_HOST_TX_PROOF'):
            module.tx_control_byte(mixed,ready,dict(op='play',proof=dict(live,rx_control=0)))

    def test_configured_native_renew_allows_null_snapshot(self):
        """Actual TxGuard CONFIGURED requests no snapshot; explicit null must not imply ownership failure."""
        from gnuradio.logicrf.usb_tx_managed import UsbTxController
        controller = UsbTxController.__new__(UsbTxController)
        controller.phase, controller.board_stopped, controller.stop_expected = 'initial', False, False
        controller._step({'state.json': None, 'stdout': ''},
                         {'tx': {'state': 'CONFIGURED', 'snapshot': None}})
        self.assertEqual(controller.phase, 'initial')

    def test_dual_rate_budget_rejected_before_credentials(self):
        """Reject dual 61.44 M aggregate demand before providers/SSH/USB without rate reduction/backend fallback."""
        called = []
        board = logicrf.make_device('usb', direction='tx', pattern='ramp32',
                                    uri='ip:offline.invalid', sample_rate=61_440_000,
                                    password_provider=lambda: called.append(True))
        graph = gr.top_block()
        for channel in (1, 2):
            graph.connect(logicrf.tx_ramp_source(board, channel), logicrf.h102_tx_sink(board, channel))
        graph.run()
        self.assertIn('USB_TX_BUDGET_EXCEEDED', board.error())
        self.assertEqual(called, [])
        self.assertIsNone(board.controller.client)

    def test_expired_cpp_lease_cancels_transport(self):
        """Without renewal the two-second local permit expires; mock control cannot fabricate permanent health."""
        board = self.device(1_048_576)
        owner = LocalOwner(board, self.root, stall=True)
        board.set_session(owner.prepare, owner.guard, owner.finish)
        graph = gr.top_block()
        graph.connect(logicrf.tx_ramp_source(board, 1), logicrf.h102_tx_sink(board, 1))
        graph.start()
        waiter = Thread(target=graph.wait, daemon=True)
        waiter.start()
        try:
            waiter.join(4)
            self.assertFalse(waiter.is_alive(), 'lease cancellation left graph waiting')
            self.assertIn('PERMISSION_EXPIRED', board.error())
            self.assertIn('usb-tx-stop', owner.calls)
        finally:
            board.shutdown()
            graph.stop()
            waiter.join(2)


if __name__ == '__main__':
    unittest.main()

# End of file: qa_usb_tx_flowgraph.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
