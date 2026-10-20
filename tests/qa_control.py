"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_control.py
@brief Verify actual PMT control dispatch, declared catalog and normal RX draining.
@details Use real GNU scheduling/libiio loopback with explicit offline control substitutes, never physical boards.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
import pmt
from gnuradio import blocks,gr,logicrf
from gnuradio.logicrf.control import dispatch
from qa_iio_protocol import Server,Handler


class ControlTests(unittest.TestCase):
    """Planned control must not claim hardware application; final counts cover every downstream sample."""
    def test_catalog_and_rx_only_plans_are_offline(self):
        catalog=logicrf.configuration_catalog();self.assertEqual(len(catalog['profiles']),58)
        self.assertEqual(len({p['id'] for p in catalog['profiles']}),58)
        board=logicrf.make_duplex_device(uri='ip:127.0.0.1',tx_channels=(),sample_rate=8e6,
            password_provider=lambda:self.fail('unexpected credentials'))
        self.assertIsNone(board.tx_native);board.set_rx_gain(1,20)
        result=dispatch(board,dict(action='select_profile',profile='0p192msps-0p1mhz-common'))
        self.assertFalse(result['hardware_applied']);self.assertEqual(board.options().sample_rate,192000)
        self.assertEqual(board.options().frame_samples,32768)
        self.assertEqual(board.controller.rx_gain_db,{'1':20})
        request=board.controller;request.info={}
        self.assertEqual(request.expected_request(board.options(),[1])['channels'],[])
        self.assertEqual(request.expected_request(board.options(),[1])['samples'],0)
        with self.assertRaises(ValueError):dispatch(board,dict(action='tune',direction='tx',frequency_hz=1e9))
        with self.assertRaises(ValueError):dispatch(board,dict(action='status',extra=True))
        board.close()

    def test_real_pmt_status_and_active_tune_refusal(self):
        board=logicrf.make_device(sample_rate=192000);source=logicrf.h102_rx_source(board,1)
        control=logicrf.h102_control(board);debug=blocks.message_debug();graph=gr.top_block()
        graph.connect(source,blocks.null_sink(gr.sizeof_gr_complex));graph.msg_connect((control,'events'),(debug,'store'))
        graph.start()
        try:
            for request in (dict(id='s',action='status'),dict(id='t',action='tune',direction='rx',frequency_hz=200e6)):
                control.to_basic_block()._post(pmt.intern('command'),pmt.to_pmt(request))
            until=time.monotonic()+2
            while debug.num_messages()<2 and time.monotonic()<until:time.sleep(.005)
            self.assertEqual(debug.num_messages(),2)
            replies=[pmt.to_python(debug.get_message(n)) for n in range(2)]
            self.assertEqual(replies[0]['status'],'ok');self.assertEqual(json.loads(replies[0]['result_json'])['plan']['backend'],'mock')
            self.assertEqual(replies[1]['status'],'error');self.assertIn('STOP_REQUIRED',replies[1]['error'])
        finally:graph.stop();graph.wait()

    def test_mock_rx_graceful_drain(self):
        board=logicrf.make_device(sample_rate=192000);sink=blocks.vector_sink_c();graph=gr.top_block()
        graph.connect(logicrf.h102_rx_source(board,1),sink);graph.start();time.sleep(.03)
        try:
            board.request_rx_stop();waiter=Thread(target=graph.wait,daemon=True);waiter.start();waiter.join(2)
            self.assertFalse(waiter.is_alive());self.assertEqual(board.error(),'')
            self.assertGreater(len(sink.data()),0);self.assertEqual(len(sink.data()),board.received(1))
            self.assertEqual(board.queue_stats(1)['output_samples'],board.received(1))
        finally:board.shutdown();graph.stop();graph.wait()

    def test_iio_graceful_stop_preserves_real_network_blocks(self):
        with Server(('127.0.0.104',30431),Handler) as endpoint:
            endpoint.mode='short';endpoint.refills=0;endpoint.index=0;endpoint.errors=[];endpoint.commands=[]
            server_thread=Thread(target=endpoint.serve_forever,kwargs=dict(poll_interval=.01),daemon=True);server_thread.start()
            options=logicrf.device_options();options.backend='iio';options.uri='ip:127.0.0.104'
            options.frame_samples=16;options.queue_frames=64;options.sample_rate=192000
            board=logicrf.device.make(options);calls=[]
            def prepare(mask):
                actual=board.options();actual.boot_id='offline';actual.metadata_json='{"hardware":false}'
                board.renew_lease();return actual
            board.set_session(prepare,lambda ch,stage:calls.append(stage),lambda:calls.append('finish'))
            sink=blocks.vector_sink_c();graph=gr.top_block();graph.connect(logicrf.h102_rx_source(board,1),sink)
            graph.start()
            try:
                until=time.monotonic()+1
                while not board.ready() and not board.error() and time.monotonic()<until:time.sleep(.001)
                self.assertTrue(board.ready());board.request_rx_stop()
                waiter=Thread(target=graph.wait,daemon=True);waiter.start();waiter.join(2)
                self.assertFalse(waiter.is_alive());self.assertEqual(board.error(),'')
                self.assertFalse(endpoint.errors);self.assertEqual(calls,['begin','arm','end','finish'])
                self.assertEqual(len(sink.data()),board.received(1))
                expected=34*16
                for value in sink.data():
                    code=expected&65535;code=code if code<32768 else code-65536
                    self.assertEqual(value,complex(code/32768,code/32768));expected+=1
            finally:
                board.shutdown();graph.stop();graph.wait();endpoint.shutdown();server_thread.join()

    def test_metadata_open_error_reaches_device(self):
        """File-open failure must propagate explicitly; an empty index is never success."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        with tempfile.TemporaryDirectory(prefix='metadata-fail-',dir=project/'tmp') as root:
            board=logicrf.make_device(sample_rate=192000,mock_samples=4096)
            index=logicrf.h102_metadata_sink(board,Path(root)/'missing'/'index',1)
            graph=gr.top_block();graph.connect(logicrf.h102_rx_source(board,1),index);graph.run()
            self.assertIn('METADATA_OPEN',index.error());self.assertIn('METADATA_OPEN',board.error())
            self.assertEqual(index.records(),0)

    def test_native_fft_example_known_tone(self):
        """Actual GNU FFT must match independent known tone amplitude/sign, vector length and frequency shift."""
        import importlib.util
        import math
        path=Path(__file__).resolve().parents[1]/'examples/fft_recording.py'
        spec=importlib.util.spec_from_file_location('record_fft_qa',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        with tempfile.TemporaryDirectory(prefix='fft-qa-',dir=project/'tmp') as root:
            raw=Path(root)/'tone.cf32'
            raw.write_bytes(b''.join(struct.pack('<ff',.25*math.cos(2*math.pi*37*n/4096),.25*math.sin(2*math.pi*37*n/4096)) for n in range(8192)))
            result,_,_=module.analyze(raw,4_096_000,1e9,4096,2)
            self.assertEqual(result['frames'],2);self.assertEqual(result['peak_offset_hz'],37000)
            self.assertAlmostEqual(result['peak_dbfs'],20*math.log10(.25),places=4)

    def test_recording_example_actual_files_and_channel_identity(self):
        """Run the recording example and verify both lengths/independent first-last IQ, beyond exit status."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        with tempfile.TemporaryDirectory(prefix='record-qa-',dir=project/'tmp') as root:
            output=Path(root)/'case';script=Path(__file__).resolve().parents[1]/'examples/record_iq.py'
            run=subprocess.run([sys.executable,'-B',str(script),'--backend','mock','--rate','192000',
                '--channels','1','2','--seconds','1','--output',str(output)],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr+run.stdout)
            result=json.loads((output/'recording.json').read_text());self.assertEqual(result['status'],'PASS_MOCK_RECORDING')
            for ch in (1,2):
                raw=(output/f'rx{ch}.cf32').read_bytes();self.assertEqual(len(raw),192000*8)
                self.assertEqual((output/f'rx{ch}.h1mi').stat().st_size,0)
                for n in (0,191999):
                    code=(n+(ch-1)*97)&65535
                    self.assertEqual(struct.unpack_from('<ff',raw,n*8),((code-32768)/32768,(32767-code)/32768))


if __name__=='__main__':unittest.main()
# End of file: qa_control.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
