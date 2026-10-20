#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_rx.py
@brief Run managed H102 IIO single/dual RX in the real GNU scheduler and verify native counts/cleanup.
@details No large IQ files; C++ Probe retains 16 prefix samples and bounded tags. Credentials are interactive/provider-only.
         TX stays off; finite capture and stop/permission-expiry faults are reported separately without RF qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.0
@date 2026-10-16
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Thread
import time


def run(args, password_provider=None):
    """Use managed Device and stop on every exception; unconfirmed independent post/windows cannot return PASS."""
    from gnuradio import gr, logicrf
    import pmt
    output=args.output.resolve();output.mkdir(mode=0o700,parents=True,exist_ok=False)
    if tuple(args.channels) not in ((1,),(2,),(1,2)):
        raise ValueError('channels must be 1, 2 or 1 2')
    if not 1<=args.seconds<=300 or args.sample_rate*4*len(args.channels)>64_000_000:
        raise ValueError('invalid duration or network budget')
    sample_limit=0 if args.timed_stop or args.inject_expiry else int(args.sample_rate*args.seconds)
    board=logicrf.make_device(backend='iio',uri='ip:'+args.host,
        configuration=args.profile,sample_rate=args.sample_rate,center_frequency=args.frequency_hz,
        sample_limit=sample_limit,
        evidence=output/'native-control',password_provider=password_provider,
        machine_id=args.machine_id,pattern=args.pattern,queue_frames=8)
    graph=gr.top_block();sinks={}
    for channel in args.channels:
        sinks[channel]=logicrf.stream_probe(args.pattern=='ramp16')
        graph.connect(logicrf.h102_rx_source(board,channel),sinks[channel])
    started=time.monotonic();waiter=None;fault_injected=False;ready_at=None;injected_at=None
    result=dict(status='RUNNING',host=args.host,profile=args.profile,channels=args.channels,
        pattern=args.pattern,requested_seconds=args.seconds,RF_TX=False,
        started_at=datetime.now(timezone.utc).isoformat())
    wait_errors=[]
    def wait_graph():
        """wait releases the GIL; propagate exceptions to the main thread for unified cleanup/records."""
        try:graph.wait()
        except Exception as error:wait_errors.append(str(error))
    try:
        graph.start()
        waiter=Thread(target=wait_graph,name='gr-iio-flowgraph-wait',daemon=True);waiter.start()
        while waiter.is_alive():
            now=time.monotonic()
            if board.error():
                if injected_at is not None:result['expiry_detected_seconds']=now-injected_at
                graph.stop();break
            if board.ready() and ready_at is None:
                ready_at=now;result['ready_seconds']=now-started
            if args.inject_expiry and ready_at and now-ready_at>=1 and not fault_injected:
                board.controller.pause_renewal=True;fault_injected=True;injected_at=now
            if args.timed_stop and ready_at and now-ready_at>=args.seconds:
                graph.stop();break
            if now-started>90+args.seconds:
                board.native.invalidate('HOST_FLOWGRAPH_DEADLINE');graph.stop();break
            time.sleep(.02)
        if waiter:waiter.join(timeout=110)
        if waiter and waiter.is_alive():raise RuntimeError('FLOWGRAPH_WAIT_NOT_FINISHED')
        if wait_errors:raise RuntimeError(';'.join(wait_errors))
    except Exception as error:result['error']=str(error)
    finally:
        cleanup=time.monotonic()
        graph.stop();graph.wait()
        try:board.close()
        except Exception as error:result['close_error']=str(error)
        result['cleanup_seconds']=time.monotonic()-cleanup
        result['native_error']=board.error()
        result['recovery_confirmed']=board.controller.recovery_confirmed
        result['control_output']=str(board.controller.run_output)
        result['guards']=board.controller.guard_records
        result['channels_result']={}
        for channel,sink in sinks.items():
            stats=sink.snapshot()
            tags={pmt.symbol_to_string(t.key):dict(offset=t.offset,value=pmt.to_python(t.value)) for t in sink.tags()}
            result['channels_result'][str(channel)]=dict(stats=stats,received=board.received(channel),queue=board.queue_stats(channel),tags=tags,
                prefix=[[v.real,v.imag] for v in sink.prefix()])
        result['total_seconds']=time.monotonic()-started
    passed=result['recovery_confirmed'] and 'error' not in result
    if args.inject_expiry:
        # Accept only the explicit permission-expiry cause, never arbitrary stream/recovery errors as expected success.
        reason=result['native_error'].split(':')[-1]
        passed=passed and fault_injected and reason in ('OWNER_PERMISSION_EXPIRED','OWNER_PERMISSION_EXPIRED_AFTER_REFILL')
        passed=passed and 0<=result.get('expiry_detected_seconds',999)<=4
        result['status']='PASS_EXPECTED_LOCAL_LEASE_EXPIRY' if passed else 'FAIL_EXPECTED_LOCAL_LEASE_EXPIRY'
    else:
        passed=passed and not result['native_error'] and 'close_error' not in result
        for channel,value in result['channels_result'].items():
            stats=value['stats'];meta=value['tags'].get('h102_config',{}).get('value',{})
            passed=passed and stats.get('samples',0)>0 and not any(stats.get(k,0) for k in ('nonfinite','out_of_range','ramp_errors','iq_mismatch'))
            if sample_limit:passed=passed and stats['samples']==sample_limit and value['received']==sample_limit
            passed=passed and meta.get('channel')==int(channel) and meta.get('backend')=='iio'
            passed=passed and value['tags'].get('rx_rate',{}).get('value')==args.sample_rate
            passed=passed and value['tags'].get('rx_freq',{}).get('value')==args.frequency_hz
            passed=passed and 'rx_time' not in value['tags'] and 'first_sample_index' not in meta
            passed=passed and result['guards'].get(channel,{}).get('end',{}).get('passed',False)
        result['status']='PASS_SCOPED_IIO_RX' if passed else 'FAIL_IIO_RX'
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status=result['status'],channels={c:v['stats'] for c,v in result['channels_result'].items()},
        native_error=result['native_error'],recovery_confirmed=result['recovery_confirmed'],seconds=result['total_seconds']),ensure_ascii=False))
    return result


def main():
    """Terminal entry; request credentials through the start provider only, never arguments/environment."""
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True)
    p.add_argument('--machine-id')
    p.add_argument('--profile',required=True);p.add_argument('--sample-rate',type=int,required=True)
    p.add_argument('--frequency-hz',type=int,default=150000000)
    p.add_argument('--channels',type=int,nargs='+',default=[1]);p.add_argument('--seconds',type=int,default=10)
    p.add_argument('--pattern',choices=('normal','ramp16'),default='normal')
    p.add_argument('--timed-stop',action='store_true');p.add_argument('--inject-expiry',action='store_true')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=run(args)
    return 0 if result['status'].startswith('PASS_') else 1


if __name__=='__main__':raise SystemExit(main())

# End of file: iio_rx.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
