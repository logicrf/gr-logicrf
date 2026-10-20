"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_iio_tx_flowgraph.py
@brief Verify finite TX bytes/batch faults through actual IIOD networking and GNU scheduling.
@details Loopback IIOD independently receives caller IQ; mock ownership never accesses boards or qualifies SSI.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-19
"""
from contextlib import contextmanager
import struct
from threading import Event,Thread
import time
import unittest
import numpy as np
from gnuradio import blocks,gr,logicrf
from qa_iio_protocol import XML,Server,Handler
from qa_tx_flowgraph import oracle_signature

TX_XML=XML[:XML.index(b'<context name=')]+b'<context name="local" version-major="0" version-minor="23" version-git="abcdefg">'+b''.join(
    b'<device id="iio:device'+str(ch-1).encode()+b'" name="'+name+b'">'
    b'<channel id="voltage0" type="output"><scan-element index="0" format="le:S16/16&gt;&gt;0"/></channel>'
    b'<channel id="voltage1" type="output"><scan-element index="1" format="le:S16/16&gt;&gt;0"/></channel></device>'
    for ch,name in enumerate((b'axi-adrv9002-tx-lpc',b'axi-adrv9002-tx2-lpc'),1))+b'</context>'


class TxHandler(Handler):
    """Actual two-phase WRITEBUF protocol with bounded independent per-device payload retention."""
    def handle(self):
        self.request.settimeout(4)
        while True:
            try:
                raw=self.rfile.readline()
                if not raw:return
                line=raw.decode().strip();server=self.server;server.commands.append(line)
                if not line:continue
                if line=='PRINT':
                    xml=TX_XML if server.mode!='bad-layout' else TX_XML.replace(b'le:S16/16',b'be:S16/16')
                    self.wfile.write(str(len(xml)).encode()+b'\n'+xml+b'\n')
                elif line=='ZPRINT':self.wfile.write(b'-22\n')
                elif line=='VERSION':self.wfile.write(b'0.23.abcdefg\n')
                elif line=='EXIT':return
                elif line.startswith('OPEN '):
                    _,device,samples,_mask=line.split();server.opens[device]=int(samples)
                    self.wfile.write(b'0\n')
                elif line.startswith(('TIMEOUT ','SET ','CLOSE ')):self.wfile.write(b'0\n')
                elif line.startswith('WRITEBUF '):
                    _,device,count=line.split();count=int(count)
                    if server.mode=='begin-error':
                        self.wfile.write(b'-5\n');self.wfile.flush();continue
                    self.wfile.write(b'0\n');self.wfile.flush()
                    data=self.rfile.read(count)
                    if len(data)!=count:return
                    server.received.setdefault(device,bytearray()).extend(data)
                    server.writes.setdefault(device,[]).append(count);server.push_started.set()
                    if server.mode in ('batch','batch-stall'):time.sleep(.02)
                    if server.mode=='playback-paced':time.sleep(count/4/384000)
                    if server.mode=='stall' or (server.mode=='batch-stall' and count>server.opens[device]*4):
                        server.batch_started.set();server.release.wait(4)
                    result=-5 if server.mode=='push-error' else count-4 if server.mode=='short-final' else count
                    self.wfile.write(str(result).encode()+b'\n')
                else:server.errors.append(line);self.wfile.write(b'-22\n')
                self.wfile.flush()
            except (OSError,ValueError,UnicodeDecodeError):return


@contextmanager
def server(mode='normal'):
    """Fixed loopback test ports only; release after threads end; never connect external devices."""
    with Server(('127.0.0.103',30431),TxHandler) as value:
        value.mode=mode;value.commands=[];value.errors=[];value.received={};value.writes={};value.opens={}
        value.push_started=Event();value.batch_started=Event();value.release=Event()
        thread=Thread(target=value.serve_forever,kwargs=dict(poll_interval=.01),daemon=True);thread.start()
        try:yield value
        finally:value.release.set();value.shutdown();thread.join()


def local_device(count=37,rate=192000,frame_samples=16,queue_frames=2):
    """Explicit mock control identity while native C++ actually uses loopback libiio networking."""
    o=logicrf.device_options();o.backend='iio';o.direction='tx';o.uri='ip:127.0.0.103'
    o.tx_samples=count;o.frame_samples=frame_samples;o.queue_frames=queue_frames;o.sample_rate=rate
    board=logicrf.device.make(o);calls=[]
    def prepare(mask):
        actual=board.options();actual.boot_id='offline-only';actual.metadata_json='{"hardware":false}'
        board.renew_lease();calls.append(('prepare',mask));return actual
    board.set_session(prepare,lambda ch,stage:calls.append((stage,ch)),lambda:calls.append(('finish',0)))
    return board,calls


class IioTxFlowgraphTests(unittest.TestCase):
    """Separate submitted counts/bytes from hardware consumption; no fault retries and bounded cleanup."""
    def test_low_rate_batch_does_not_exceed_response_deadline(self):
        """A 384 kSPS block takes about 171 ms; eight-block batches must not push final replies beyond one second."""
        count=8*65536+17
        with server('playback-paced') as endpoint:
            board,_=local_device(count,384000,65536,8);graph=gr.top_block()
            graph.connect(blocks.vector_source_c([.125j]*count,False),logicrf.h102_tx_sink(board,1))
            graph.start()
            try:
                until=time.monotonic()+4
                while board.tx_stats(1)['iio_submitted_samples']<count and not board.error():
                    self.assertLess(time.monotonic(),until)
                    board.renew_lease();time.sleep(.05)
                graph.wait();self.assertEqual(board.error(),'')
            finally:board.shutdown();graph.stop();graph.wait()
            self.assertEqual(max(endpoint.writes['iio:device0']),65536*4)
            self.assertEqual(board.tx_stats(1)['iio_submitted_samples'],count)
            self.assertEqual(board.tx_stats(1)['iio_uncertain_samples'],0)

    def test_batch_small_dma_exact_payload_and_tail(self):
        """OPEN stays 65536; one WRITEBUF spans actual DMA blocks without padding short tails."""
        count=12*65536+17
        with server('batch') as endpoint:
            board,calls=local_device(count,8_000_000,65536,8);graph=gr.top_block()
            graph.connect(blocks.vector_source_c([.25+.125j]*count,False),logicrf.h102_tx_sink(board,1))
            graph.run();self.assertEqual(board.error(),'');self.assertFalse(endpoint.errors)
            self.assertEqual(endpoint.opens['iio:device0'],65536)
            self.assertIn('SET iio:device0 BUFFERS_COUNT 16',endpoint.commands)
            self.assertEqual([x for x in endpoint.commands if 'BUFFERS_COUNT' in x][-1],
                             'SET iio:device0 BUFFERS_COUNT 4')
            self.assertTrue(any(n>65536*4 for n in endpoint.writes['iio:device0']))
            self.assertEqual(endpoint.received['iio:device0'],struct.pack('<hh',8192,4096)*count)
            stats=board.tx_stats(1);self.assertEqual(stats['iio_submitted_samples'],count)
            self.assertEqual(stats['iio_uncertain_samples'],0);self.assertEqual(stats['finite_complete'],1)

    def test_begin_rejection_and_short_final_are_not_success(self):
        """Send no IQ after initial rejection; short final replies leave entire blocks uncertain, without retry."""
        for mode,error in [('begin-error','IIO_TX_WRITE_BEGIN:-5'),('short-final','IIO_TX_PUSH_LENGTH:60')]:
            with server(mode) as endpoint:
                board,_=local_device();graph=gr.top_block()
                graph.connect(blocks.vector_source_c([.25j],True),logicrf.h102_tx_sink(board,1))
                graph.run();self.assertIn(error,board.error());stats=board.tx_stats(1)
                self.assertEqual(stats['iio_submitted_samples'],0);self.assertEqual(stats['finite_complete'],0)
                self.assertEqual(stats['iio_uncertain_samples'],16)
                self.assertEqual(len(endpoint.received.get('iio:device0',b'')),0 if mode=='begin-error' else 64)

    def test_cancel_pending_multi_dma_response_is_bounded_and_uncertain(self):
        """Cancellation after payload transmission but before final reply never marks the batch successful."""
        with server('batch-stall') as endpoint:
            board,_=local_device(0,8_000_000,65536,8);graph=gr.top_block()
            graph.connect(blocks.vector_source_c([.125j],True),logicrf.h102_tx_sink(board,1))
            graph.start()
            try:
                self.assertTrue(endpoint.batch_started.wait(2));start=time.monotonic();board.shutdown()
                graph.stop();graph.wait();self.assertLess(time.monotonic()-start,1.5)
                stats=board.tx_stats(1);self.assertGreater(stats['iio_uncertain_samples'],65536)
                self.assertEqual(stats['iio_uncertain_samples'],endpoint.writes['iio:device0'][-1]//4)
                self.assertEqual(stats['finite_complete'],0)
            finally:board.shutdown();graph.stop();graph.wait()
            self.assertEqual([x for x in endpoint.commands if 'BUFFERS_COUNT' in x][-1],
                             'SET iio:device0 BUFFERS_COUNT 4')

    def test_dual_tail_and_restart(self):
        """Independent dual payloads and actual 20-byte tails; graph restart never reuses old counts."""
        board,calls=local_device();graph=gr.top_block();sources=[];raws={}
        for ch in (1,2):
            codes=[(n*7+ch*97-16384,n*3-8192) for n in range(37)]
            raws['iio:device'+str(ch-1)]=b''.join(struct.pack('<hh',i,q) for i,q in codes)
            source=blocks.vector_source_c([complex(i/32768,q/32768) for i,q in codes],False)
            sources.append(source);graph.connect(source,logicrf.h102_tx_sink(board,ch))
        for repeat in range(2):
            with server() as endpoint:
                for source in sources:source.rewind()
                graph.run();self.assertEqual(board.error(),'');self.assertFalse(endpoint.errors)
                self.assertEqual(endpoint.received,raws)
                for ch in (1,2):
                    stats=board.tx_stats(ch);raw=raws['iio:device'+str(ch-1)]
                    self.assertEqual(endpoint.writes['iio:device'+str(ch-1)],[64,64,20])
                    self.assertEqual(stats['accepted_samples'],37);self.assertEqual(stats['iio_submitted_samples'],37)
                    self.assertEqual(stats['signature'],oracle_signature([v[0] for v in struct.iter_unpack('<I',raw)]))
                    self.assertEqual(stats['finite_complete'],1);self.assertEqual(stats['ssi_verified_samples'],0)
                    self.assertEqual(stats['mock_consumed_samples'],0);self.assertEqual(stats['iio_uncertain_samples'],0)
                self.assertEqual(sum(1 for stage,ch in calls if stage=='finish'),repeat+1)

    def test_continuous_natural_eof_and_graceful_stop(self):
        """Undeclared lengths preserve complete EOF tails; active stop neither pads nor replays."""
        with server() as endpoint:
            board,calls=local_device(0);graph=gr.top_block()
            graph.connect(blocks.vector_source_c([.25-.5j]*37,False),logicrf.h102_tx_sink(board,1))
            graph.run();self.assertEqual(board.error(),'')
            self.assertEqual(endpoint.received['iio:device0'],struct.pack('<hh',8192,-16384)*37)
            self.assertEqual(board.tx_stats(1)['iio_submitted_samples'],37)
            self.assertEqual(board.tx_stats(1)['finite_complete'],1)
        with server() as endpoint:
            board,calls=local_device(0);graph=gr.top_block()
            for ch in (1,2):graph.connect(blocks.vector_source_c([.125j],True),logicrf.h102_tx_sink(board,ch))
            graph.start()
            try:
                self.assertTrue(endpoint.push_started.wait(2))
                target=board.request_tx_stop();graph.wait()
                self.assertEqual(board.error(),'')
                for ch in (1,2):
                    self.assertEqual(board.tx_stats(ch)['iio_submitted_samples'],target)
                    self.assertEqual(board.tx_stats(ch)['finite_complete'],1)
                    self.assertEqual(len(endpoint.received['iio:device'+str(ch-1)]),target*4)
            finally:board.shutdown();graph.stop();graph.wait()

    def test_error_and_layout_are_not_submission_success(self):
        """Negative push retains uncertain blocks; invalid scans fail before any IQ transmission."""
        for mode,reason in (('push-error','IIO_TX_PUSH_LENGTH:-5'),('bad-layout','IIO_TX_SCAN_FORMAT')):
            with server(mode) as endpoint:
                board,calls=local_device();graph=gr.top_block()
                graph.connect(blocks.vector_source_c([.25-.5j],True),logicrf.h102_tx_sink(board,2))
                graph.run();self.assertIn(reason,board.error());stats=board.tx_stats(2)
                self.assertEqual(stats['iio_submitted_samples'],0);self.assertEqual(stats['finite_complete'],0)
                self.assertEqual(stats['iio_uncertain_samples'],16 if mode=='push-error' else 0)
                self.assertEqual(len(endpoint.writes.get('iio:device1',[])),1 if mode=='push-error' else 0)
                self.assertEqual(calls.count(('finish',0)),1)

    def test_cancel_blocked_push_and_gil(self):
        """Cancel blocked push without GIL-held joins; unknown submitted suffixes remain uncertain."""
        with server('stall') as endpoint:
            board,calls=local_device(10000);graph=gr.top_block()
            graph.connect(blocks.vector_source_c([0j],True),logicrf.h102_tx_sink(board,1))
            graph.start()
            try:
                self.assertTrue(endpoint.push_started.wait(2));start=time.monotonic();board.shutdown()
                graph.stop();graph.wait();self.assertLess(time.monotonic()-start,1.5)
                self.assertIn('CANCELLED',board.error());self.assertEqual(board.tx_stats(1)['finite_complete'],0)
                self.assertEqual(board.tx_stats(1)['iio_uncertain_samples'],16)
                self.assertEqual(calls.count(('finish',0)),1)
            finally:board.shutdown();graph.stop();graph.wait()

    def test_budget_and_missing_owner_before_control(self):
        """Over-budget dual RX/missing ownership needs no credentials, SSH or actual libiio connection."""
        for rate,reason in ((8_000_000,'MANAGED_TX_SESSION_REQUIRED'),(16_000_000,'IIO_TX_BUDGET_EXCEEDED')):
            o=logicrf.device_options();o.backend='iio';o.direction='tx';o.sample_rate=rate
            board=logicrf.device.make(o);graph=gr.top_block()
            for ch in (1,2):graph.connect(blocks.vector_source_c([0j],True),logicrf.h102_tx_sink(board,ch))
            graph.run();self.assertIn(reason,board.error())

    def test_expired_lease_without_input(self):
        """When connected upstream stops producing IQ, C++ still revokes expired permission without waiting for new input."""
        class Paused(gr.sync_block):
            def __init__(self):gr.sync_block.__init__(self,name='iio_tx_paused',in_sig=None,out_sig=[np.complex64])
            def work(self,inputs,outputs):return 0
        with server():
            board,calls=local_device(10000);graph=gr.top_block();source=Paused()
            graph.connect(source,logicrf.h102_tx_sink(board,1));graph.start()
            try:
                until=time.monotonic()+3
                while not board.error() and time.monotonic()<until:time.sleep(.01)
                self.assertIn('IIO_TX_PERMISSION_EXPIRED',board.error())
            finally:board.shutdown();graph.stop();graph.wait()
            self.assertEqual(board.tx_stats(1)['iio_submitted_samples'],0)
            self.assertEqual(calls.count(('finish',0)),1)


if __name__=='__main__':unittest.main()
# End of file: qa_iio_tx_flowgraph.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
