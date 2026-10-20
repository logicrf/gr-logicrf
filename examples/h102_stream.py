#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file h102_stream.py
@brief Demonstrate Ethernet/USB single/dual RX, single/dual TX and supported mixed sessions.
@details GNU C++ generates tones and processes IQ; Python configures, inspects counts and requests normal stop.
         RF defaults off; verify loopback wiring/attenuation before hardware use. Zero seconds runs until Ctrl-C.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import argparse
import json
import math
import signal
from threading import Event,Thread
import time
from gnuradio import blocks,gr,logicrf


def main():
    """Retain the shared Device until graph exit; errors/independent recovery failures cause a nonzero exit."""
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=('iio','usb'),default='iio')
    parser.add_argument('--host',required=True);parser.add_argument('--profile',default='4msps-2mhz-common')
    parser.add_argument('--rate',type=int,default=4_000_000);parser.add_argument('--frequency',type=int,default=1_000_000_000)
    parser.add_argument('--rx',nargs='*',type=int,default=[1]);parser.add_argument('--tx',nargs='*',type=int,default=[])
    parser.add_argument('--gain',type=float,default=20);parser.add_argument('--attenuation',type=float,default=40)
    parser.add_argument('--rf',action='store_true');parser.add_argument('--seconds',type=float,default=10)
    args=parser.parse_args()
    if args.seconds<0:parser.error('seconds must be nonnegative')
    board=logicrf.make_duplex_device(backend=args.backend,uri='ip:'+args.host,
        rx_channels=tuple(args.rx),tx_channels=tuple(args.tx),sample_rate=args.rate,configuration=args.profile,
        rx_frequency=args.frequency,tx_frequency=args.frequency,rx_gain_db={ch:args.gain for ch in args.rx},
        attenuation_db=args.attenuation,rf_enabled=args.rf,tx_samples=0,usb_seconds=0)
    stop=Event();previous={s:signal.signal(s,lambda *_:stop.set()) for s in (signal.SIGINT,signal.SIGTERM)}
    graph=gr.top_block();probes={};sources=[];errors=[];waiter=None;ready_at=None;requested=False
    try:
        for ch in args.rx:
            probes[ch]=logicrf.stream_probe(False);graph.connect(logicrf.h102_rx_source(board,ch),probes[ch])
        for ch in args.tx:
            wave=[10**(-12/20)*complex(math.cos(2*math.pi*ch*n/64),math.sin(2*math.pi*ch*n/64)) for n in range(64)]
            source=blocks.vector_source_c(wave,True);sources.append(source);graph.connect(source,logicrf.h102_tx_sink(board,ch))
        graph.start();waiter=Thread(target=graph.wait,daemon=True);waiter.start();startup=time.monotonic()
        while waiter.is_alive():
            if board.error():raise RuntimeError(board.error())
            now=time.monotonic()
            if board.ready() and ready_at is None:
                ready_at=now;print(json.dumps(dict(event='ready',status=logicrf.device_status(board))),flush=True)
            if not ready_at and now-startup>125:raise TimeoutError('START_DEADLINE')
            if ready_at and not requested and (stop.is_set() or (args.seconds and now-ready_at>=args.seconds)):
                if args.tx:board.stop_tx()
                if args.rx and (args.backend=='iio' or not args.tx):board.stop_rx()
                requested=True;stop_time=now
            if requested and now-stop_time>80:raise TimeoutError('STOP_DEADLINE')
            time.sleep(.03)
        board.close()
        if not board.controller.recovery_confirmed:raise RuntimeError('RECOVERY_NOT_CONFIRMED')
        print(json.dumps(dict(event='complete',rx={str(ch):probe.snapshot() for ch,probe in probes.items()},
            tx={str(ch):board.tx_stats(ch) for ch in args.tx},status=logicrf.device_status(board))),flush=True)
    except Exception as error:
        errors.append(repr(error));print(json.dumps(dict(event='error',error=repr(error))),flush=True)
    finally:
        if errors:
            try:board.shutdown()
            except Exception as error:errors.append(repr(error))
        graph.stop()
        if waiter:waiter.join(90)
        try:board.close()
        except Exception as error:errors.append(repr(error))
        for signum,handler in previous.items():signal.signal(signum,handler)
    return int(bool(errors))


if __name__=='__main__':raise SystemExit(main())
# End of file: h102_stream.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
