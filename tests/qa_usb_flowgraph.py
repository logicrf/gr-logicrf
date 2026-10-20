"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_flowgraph.py
@brief Test actual C++ USB receiver, dual Sources, IQ and frame tags with synthetic local wire data.
@details No D3XX loading or USB access; byte services are offline fixtures, not production IQ bridges.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.3.0
@date 2026-10-16
"""
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
from threading import Thread
import time
import unittest
from gnuradio import gr,logicrf


def configuration():
    """Independently construct dual 112-byte expectations with synthetic MAC/stream/configuration identity."""
    lane=struct.pack('<QIIQIiIQQBBBB',8_000_000,1,4_000_000,2_400_000_000,29,0,0x2f,1234,43,2,0,0,0)
    return lane*2


def frame(channel,sequence):
    """Construct complete 2048-IQ frames with incrementing I=Q and independently encoded field positions."""
    h=bytearray(128);h[:8]=b'H102'+bytes((3,1,channel,1));struct.pack_into('<H',h,8,128)
    h[10]=int(sequence==0)
    for offset in (13,14,15,110,111):h[offset]=1
    for offset,value,kind in ((16,8192,'I'),(20,sequence,'I'),(24,sequence*2048,'Q'),(32,43,'Q'),
            (40,29,'I'),(44,29,'I'),(48,8_000_000,'Q'),(56,1,'I'),(60,2048,'I'),
            (64,2_400_000_000,'Q'),(72,4_000_000,'I'),(80,1792100000,'Q'),
            (88,sequence*4096,'I'),(92,16_000_000,'I'),(96,1234,'Q')):
        struct.pack_into('<'+kind,h,offset,value)
    h[104]=2
    for offset in (105,106,107,109):h[offset]=1
    payload=b''.join(struct.pack('<HH',(sequence*2048+n)%65536,(sequence*2048+n)%65536) for n in range(2048))
    return bytes(h)+payload


class UsbFlowgraph(unittest.TestCase):
    """Offline end-to-end checks of actual C++ socket/parser/queues/Source/Probe."""
    def test_dual_fragmented_wire(self):
        """Arbitrary byte chunks must deliver exact dual-Source IQ and native index/time tags."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT']);parent=project/'tmp';parent.mkdir(exist_ok=True)
        # UNIX address limits are separate from data paths; long customer workspaces must still run offline checks.
        with tempfile.TemporaryDirectory(prefix='usb-offline-',dir=parent) as raw, \
             tempfile.TemporaryDirectory(prefix='h102-wire-',dir='/tmp') as socket_root:
            path=Path(raw);config=path/'configuration.bin';config.write_bytes(configuration())
            options=logicrf.device_options();options.backend='usb';options.sample_rate=8e6
            options.center_frequency=2.4e9;options.rf_bandwidth=4e6;options.queue_frames=128
            options.usb_socket=str(Path(socket_root)/'wire.sock');options.usb_configuration=str(config)
            options.usb_stream_id=43;options.usb_time_epoch=29
            native=logicrf.device.make(options)
            def prepare(mask):
                self.assertEqual(mask,3);options.boot_id='offline';options.metadata_json='{"offline":true}'
                native.renew_lease();return options
            native.set_session(prepare,lambda channel,stage:None,lambda:None)
            failures=[]
            wire=b''.join(frame(ch,seq) for seq in range(1024) for ch in (1,2))
            def producer():
                try:
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                        until=time.monotonic()+5
                        while True:
                            try:client.connect(options.usb_socket);break
                            except FileNotFoundError:
                                if time.monotonic()>until:raise
                                time.sleep(.005)
                        # Simulate over 100 ms without data after actual board READY during control setup.
                        time.sleep(.35)
                        for off in range(0,len(wire),65533):client.sendall(wire[off:off+65533])
                except Exception as e:failures.append(repr(e))
            thread=Thread(target=producer,daemon=True);thread.start()
            graph=gr.top_block();probes=[];indexes=[]
            for ch in (1,2):
                source=logicrf.h102_rx_source(native,ch);probe=logicrf.stream_probe(True)
                graph.connect(source,probe);probes.append(probe)
                index=logicrf.h102_metadata_sink(native,path/f'rx{ch}.h1mi',ch)
                graph.connect(source,index);indexes.append(index)
            graph.run();native.shutdown();thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(native.error(),'', 'native receiver failure; producer='+repr(failures))
            self.assertEqual(failures,[])
            for ch,probe in enumerate(probes,1):
                stats=probe.snapshot()
                self.assertEqual(stats['samples'],2097152);self.assertEqual(native.received(ch),2097152)
                self.assertEqual(stats['ramp_errors'],0);self.assertEqual(stats['iq_mismatch'],0)
                self.assertEqual(stats['usb_frame_tags'],1024);self.assertEqual(stats['usb_tagged_samples'],2097152)
                self.assertEqual(stats.get('usb_tag_errors',0),0)
                self.assertEqual(indexes[ch-1].error(),'');self.assertEqual(indexes[ch-1].records(),1024)
                index=(path/f'rx{ch}.h1mi').read_bytes();self.assertEqual(len(index),1024*128)
                for sequence in range(1024):
                    row=index[sequence*128:(sequence+1)*128]
                    self.assertEqual(row[:8],b'H1MI'+bytes((1,ch,1,2)))
                    self.assertEqual(struct.unpack_from('<QQQ',row,8),(sequence*2048,sequence*2048,1792100000))
                    self.assertEqual(struct.unpack_from('<IIIIII',row,32),(sequence*4096,16000000,sequence,29,2048,29))
                    self.assertEqual(row[100:],bytes(28))

if __name__=='__main__':unittest.main()
# End of file: qa_usb_flowgraph.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
