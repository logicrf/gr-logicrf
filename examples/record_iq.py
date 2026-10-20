#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file record_iq.py
@brief Record C++ cf32 IQ, USB integer frame indices and verified session metadata.
@details Remove partial status only after normal completion, count/file/write/native-guard checks and SHA hashing.
         IIO has no native ADC sequence/UTC; file-length checks do not calibrate absolute RF time.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event,Thread
import time
from gnuradio import blocks,gr,logicrf


def digest(path):
    """Stream full-file SHA with at most 1 MiB memory after acquisition ends."""
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1<<20),b''):value.update(chunk)
    return value.hexdigest()


def run(args,password_provider=None,host_password_provider=None):
    """Run one case; retain partial/failure metadata and deliver received queues fully on normal completion."""
    if sys.byteorder!='little':raise RuntimeError('CF32_LE_HOST_REQUIRED')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    channels=tuple(args.channels);points=int(args.rate*args.seconds);graph=None;board=None;waiter=None
    files={};indexes={};errors=[];stop=Event();previous={};start=time.monotonic();ready_at=None;stop_sent=False
    result=dict(status='RUNNING',backend=args.backend,profile=args.profile,rate_hz=args.rate,
        center_frequency_hz=args.frequency,channels=channels,continuous=args.continuous,
        format='cf32_le',frame_index='H1MI-v1-128',RF_TX=False,utc_supported=args.backend=='usb')
    for signum in (signal.SIGINT,signal.SIGTERM):previous[signum]=signal.signal(signum,lambda *_:stop.set())
    try:
        common=dict(sample_rate=args.rate,configuration=args.profile,uri='ip:'+args.host,
            sample_limit=0 if args.continuous else points,password_provider=password_provider,
            evidence=args.output/'control')
        if args.backend=='iio':
            board=logicrf.make_duplex_device(rx_channels=channels,tx_channels=(),rx_frequency=args.frequency,
                rx_gain_db={ch:args.gain for ch in channels},**common)
        elif args.backend=='usb':
            common['sample_limit']=0
            board=logicrf.make_duplex_device(backend='usb',rx_channels=channels,tx_channels=(),
                rx_frequency=args.frequency,rx_gain_db={ch:args.gain for ch in channels},usb_seconds=0,
                host_password_provider=host_password_provider,**common)
        else:board=logicrf.make_device('mock',sample_rate=args.rate,center_frequency=args.frequency,
                    mock_samples=0 if args.continuous else points)
        graph=gr.top_block()
        for ch in channels:
            source=logicrf.h102_rx_source(board,ch)
            files[ch]=blocks.file_sink(gr.sizeof_gr_complex,str(args.output/f'rx{ch}.cf32.partial'),False)
            indexes[ch]=logicrf.h102_metadata_sink(board,args.output/f'rx{ch}.h1mi.partial',ch)
            graph.connect(source,files[ch]);graph.connect(source,indexes[ch])
        graph.start();waiter=Thread(target=graph.wait,daemon=True);waiter.start()
        while waiter.is_alive():
            if board.error():raise RuntimeError(board.error())
            if any(index.error() for index in indexes.values()):raise RuntimeError('FRAME_INDEX_IO:'+str([i.error() for i in indexes.values()]))
            now=time.monotonic()
            if board.ready() and ready_at is None:ready_at=now
            timed_usb_stop=args.backend=='usb' and not args.continuous and ready_at and now-ready_at>=args.seconds
            if (stop.is_set() or timed_usb_stop) and not stop_sent and ready_at:
                if hasattr(board,'stop_rx'):board.stop_rx()
                else:board.request_rx_stop()
                stop_sent=True
            if not args.continuous and now-start>args.seconds+130:raise TimeoutError('RECORDING_DEADLINE')
            time.sleep(.02)
        board.close() if hasattr(board,'close') else board.shutdown()
        if board.error():raise RuntimeError(board.error())
    except Exception as error:errors.append(repr(error))
    finally:
        if errors and board:
            try:board.shutdown()
            except Exception as error:errors.append('shutdown:'+repr(error))
        if graph:graph.stop()
        if waiter:
            waiter.join(90)
            if waiter.is_alive():errors.append('FLOWGRAPH_NOT_JOINED')
        for sink in files.values():
            try:sink.close()
            except Exception as error:errors.append('file-close:'+repr(error))
        if board:
            try:board.close() if hasattr(board,'close') else board.shutdown()
            except Exception as error:errors.append('close:'+repr(error))
            result['device']=logicrf.device_status(board)
        for signum,handler in previous.items():signal.signal(signum,handler)
    result['files']={}
    try:
        if errors:raise RuntimeError(';'.join(errors))
        controller=getattr(board,'controller',None)
        if args.backend=='usb':
            result['stop_boundary']=dict(kind='complete_frames_before_protocol_stop',queue=
                {str(ch):board.queue_stats(ch) for ch in channels},adc_sample_exact_endpoint=False)
        if args.backend!='mock' and not controller.recovery_confirmed:raise RuntimeError('INDEPENDENT_RECOVERY_NOT_CONFIRMED')
        for ch in channels:
            path=args.output/f'rx{ch}.cf32.partial';index_path=args.output/f'rx{ch}.h1mi.partial'
            with path.open('rb') as stream:os.fsync(stream.fileno())
            samples=board.received(ch);written=files[ch].nitems_read(0)
            if samples!=written or path.stat().st_size!=samples*8 or not samples:raise RuntimeError('IQ_COUNT_OR_FILE_SIZE')
            if args.backend!='usb' and not args.continuous and samples!=points:raise RuntimeError('IQ_FINITE_LENGTH')
            records=indexes[ch].records()
            if indexes[ch].error() or index_path.stat().st_size!=records*128:raise RuntimeError('FRAME_INDEX_IO_OR_SIZE')
            if args.backend=='usb' and records*2048!=samples:raise RuntimeError('USB_FRAME_INDEX_COVERAGE')
            if args.backend=='iio' and not controller.guard_records.get('rx'+str(ch),{}).get('end',{}).get('passed'):
                raise RuntimeError('ADC_DMA_OVERFLOW_WINDOW_NOT_VERIFIED')
            result['files'][str(ch)]=dict(samples=samples,bytes=samples*8,sha256=digest(path),
                frame_records=records,frame_index_sha256=digest(index_path))
        # Declare recording-set success only after checking every file and both directions' cleanup; partial failure is not a usable set.
        for ch in channels:
            for extension in ('cf32','h1mi'):
                path=args.output/f'rx{ch}.{extension}.partial';path.rename(path.with_suffix(''))
        result['status']='PASS_MOCK_RECORDING' if args.backend=='mock' else 'PASS_SCOPED_IQ_RECORDING'
    except Exception as error:
        if not errors:errors.append(repr(error))
        result['status']='FAIL'
    result.update(errors=errors,elapsed_seconds=time.monotonic()-start)
    (args.output/'recording.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True);return result


def main():
    """Default finite IIO recording; Ctrl-C requests normal continuous-mode sealing; abort never marks completion."""
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=('mock','iio','usb'),default='iio')
    parser.add_argument('--host',default='192.0.2.10');parser.add_argument('--profile',default='8msps-4mhz-common')
    parser.add_argument('--rate',type=int,default=8_000_000);parser.add_argument('--frequency',type=int,default=1_000_000_000)
    parser.add_argument('--gain',type=float,default=20);parser.add_argument('--channels',nargs='+',type=int,default=[1])
    parser.add_argument('--seconds',type=int,default=2);parser.add_argument('--continuous',action='store_true')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if not 1<=args.seconds<=300 or tuple(args.channels) not in ((1,),(2,),(1,2)):parser.error('CHANNEL_OR_DURATION')
    result=run(args);return int(result['status']=='FAIL')


if __name__=='__main__':raise SystemExit(main())
# End of file: record_iq.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
