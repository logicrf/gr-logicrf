#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file board_iio_tx.py
@brief Independent native permission/post recovery for RF-off GNU Radio IIO TX.
@details Root temporary sessions reuse pinned hardware tools/SSH protocol; PID1 retains three locks through post.
         Allow selected TX DMA only, without RF/DDS, cyclic TX or released-script modifications.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.6.0
@date 2026-10-16
@note Permission establishes configuration/source silence, not SSI consumption of network push.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time
import uuid

import board_tx as common
from board_tx import radio

SOURCES=('board_iio_tx.py','board_tx.py','board_radio.py','tx_cyclic.py')


def validate(expected):
    """Independently check channels, finite lengths and budgets on-board beyond host admission checks."""
    channels=expected['channels'];samples=expected['samples'];rate=expected['rate']
    if channels not in ([1],[2],[1,2]) or type(samples) is not int or samples<0:
        raise ValueError('IIO_TX_CHANNELS_OR_LENGTH')
    if not isinstance(rate,(int,float)) or not math.isfinite(rate) or rate<=0 or rate*4*len(channels)>64_000_000:
        raise ValueError('IIO_TX_NETWORK_BUDGET')
    if samples/rate>300:raise ValueError('IIO_TX_FINITE_DURATION')
    return math.ceil(samples/rate)+35 if samples else 0


def sources_off(hw,channels):
    """Require RF/DDS/RX/nonselected TX off; RF state never substitutes for DMA state."""
    paths=[hw.phy/f'out_voltage{i}_en' for i in (0,1)]
    paths += [hw.alt(n,i,'raw') for n in common.DDS for i in range(4)]
    names=('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc')
    names+=tuple(n for c,n in enumerate(common.DDS,1) if c not in channels)
    paths += [hw.devices[n]/'buffer/enable' for n in names]
    values={str(p):hw.read(p) for p in paths}
    if any(v!='0' for v in values.values()):raise RuntimeError('IIO_TX_FOREIGN_OR_RF_SOURCE')
    return values


def load(directory):
    """Independent post verifies requests/frozen code; unknown source sets cannot participate in recovery."""
    c,rx,_=radio.modules();c.private(directory,directory=True);c.private(directory/'request.json')
    request=c.read_json(directory/'request.json')
    if set(request['sources'])!=set(SOURCES):raise ValueError('IIO_TX_SOURCE_SET')
    for name,digest in request['sources'].items():
        c.private(directory/name)
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:raise ValueError('IIO_TX_SOURCE_CHANGED')
    return c,rx,request


def actor(directory):
    """Actor retains three locks/15-second heartbeat; persist immutable recovery requests before data permission."""
    c,rx,_=radio.modules();c.private(directory,directory=True);c.private(directory/'expected.json')
    expected=c.read_json(directory/'expected.json');seconds=validate(expected);channels=expected['channels']
    controller=c.read_json(directory/'controller.json');files=[];hw=None;request=None
    def interrupted(signum,frame):raise TimeoutError('IIO_TX_NATIVE_INTERRUPTED')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGALRM,interrupted);signal.alarm(85)
    try:
        for name in common.LOCKS:
            f=open('/run/lock/'+name,'a+b');files.append(f);until=time.monotonic()+3
            while True:
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if name!=common.LOCKS[0] or time.monotonic()>=until:raise RuntimeError('IIO_TX_DEVICE_BUSY_NO_WRITES')
                    time.sleep(.02)
        identity=c.identity();hw=c.rf.Hardware(directory,identity,None);hw.idle();rx.stopped_tx(hw)
        if (Path('/sys/kernel/debug/iio')/hw.phy.name/'api_version').read_text().strip()!=c.API:
            raise ValueError('IIO_TX_DRIVER_API')
        state=c.read_json(c.STATE);actual=c.snapshot(hw);info=expected['info']
        package=c.load_package(c.CATALOG,state['profile_id'],trusted=True);c.verify_snapshot(actual,package)
        if (identity!=state['identity'] or actual!=state['actual'] or state['sha256']!=package['manifest']['sha256']
                or info['boot_id']!=identity['boot_id'] or info['configuration_id']!=state['configuration_id']
                or info['active']!=state['profile_id'] or info['parameters']!=actual['parameters']
                or info['radio']!=radio.describe(hw,state,c.CATALOG)):
            raise ValueError('IIO_TX_EXPECTED_CONFIGURATION_CHANGED')
        if set(rx.ssi_modes(hw).values())!={'TESTMODE_DATA_NORMAL'}:raise ValueError('IIO_TX_SSI_NOT_NORMAL')
        params={v['channel']:v for v in actual['parameters']['tx']}
        if any(params[ch]['sample_rate_hz']!=expected['rate'] or
               info['radio']['tx_center_frequency_hz'][str(ch)]!=expected['frequency'] for ch in channels):
            raise ValueError('IIO_TX_RATE_OR_FREQUENCY_CHANGED')
        import h102_iio_tx_lease as dma
        request=dict(unit=controller['unit'],invocation=os.environ['INVOCATION_ID'],identity=identity,
            actual=actual,configuration_id=state['configuration_id'],channels=channels,settings=hw.settings(),
            dma_settings=dma.dma_settings(hw),ssi_modes=rx.ssi_modes(hw),
            sources={n:hashlib.sha256((directory/n).read_bytes()).hexdigest() for n in SOURCES})
        common.notify('FDSTORE=1\nFDPOLL=0\nFDNAME=h102-board-locks',[f.fileno() for f in files])
        until=time.monotonic()+3
        while common.unit_status(request['unit'])['NFileDescriptorStore']!='3' and time.monotonic()<until:time.sleep(.05)
        common.check_store(request);radio.save(directory/'request.json',request)
        issued=time.monotonic_ns();ready=dict(pid=os.getpid(),issued_ns=issued,deadline_ns=issued+seconds*10**9 if seconds else 0,
            identity=identity,configuration_id=state['configuration_id'],RF_TX=False,channels=channels)
        radio.save(directory/'ready.json',ready);signal.alarm(seconds+2 if seconds else 0)
        next_check=0
        while not (directory/'stop').exists():
            heartbeat=c.read_json(directory/'heartbeat.json');now=time.monotonic_ns();common.heartbeat_age(heartbeat,now)
            if ready['deadline_ns'] and now>=ready['deadline_ns']:raise TimeoutError('IIO_TX_TASK_DEADLINE')
            if time.monotonic()>=next_check:
                common.check_store(request);hw.verify(complete=False);sources_off(hw,channels)
                if c.snapshot(hw)!=actual or rx.ssi_modes(hw)!=request['ssi_modes']:
                    raise RuntimeError('IIO_TX_ACTIVE_CONFIGURATION_CHANGED')
                # Keep non-enable settings unchanged; sources_off and target-buffer readback independently verify source enables.
                current=hw.settings();baseline=dict(request['settings'])
                for ch in channels:
                    key=str(hw.devices[common.DDS[ch-1]]/'buffer/enable')
                    current.pop(key,None);baseline.pop(key,None)
                if current!=baseline:raise RuntimeError('IIO_TX_ACTIVE_SETTINGS_CHANGED')
                rx.atomic_save(directory/'health.json',dict(checked_ns=now,permitted=True,
                    target_buffers={str(ch):hw.read(hw.devices[common.DDS[ch-1]]/'buffer/enable') for ch in channels}),exclusive=False)
                next_check=time.monotonic()+.5
            common.notify('READY=1\nWATCHDOG=1');time.sleep(.1)
        radio.save(directory/'main.json',dict(status='stopped',RF_TX=False))
    except Exception as error:
        if not (directory/'main.json').exists():radio.save(directory/'main.json',dict(status='failed',error=str(error)))
        raise
    finally:
        signal.alarm(0)
        for f in files:f.close()  # PID1 retains the same OFDs through independent post; never LOCK_UN.


def post(directory):
    """Stop sources/restore DMA while PID1 retains three locks; preserve original errors."""
    if not (directory/'request.json').exists():return
    c,rx,request=load(directory);common.check_store(request,post=True)
    if c.identity()!=request['identity']:raise ValueError('IIO_TX_POST_IDENTITY')
    hw=c.rf.Hardware(directory,request['identity'],None);errors=hw.stop()
    import h102_iio_tx_lease as dma
    restored=dma.restore_dma(hw,request['dma_settings']);errors+=restored['errors']
    try:
        buffers=rx.restore_network_kernel_buffers();hw.idle();sources_off(hw,[])
    except Exception as error:errors.append(str(error));buffers=None
    result=dict(passed=not errors,errors=errors,RF_TX=False,dma_restore=restored,
        settings_restored=hw.settings()==request['settings'],configuration_unchanged=c.snapshot(hw)==request['actual'],
        ssi_unchanged=rx.ssi_modes(hw)==request['ssi_modes'],network_kernel_buffers=buffers,
        lock_store=common.unit_status(request['unit']))
    result['passed']=all((result['passed'],restored['passed'],result['settings_restored'],
        result['configuration_unchanged'],result['ssi_unchanged']))
    radio.save(directory/'post.json',result)
    if not result['passed']:raise RuntimeError('IIO_TX_POST_RECOVERY_FAILED')


def control(directory):
    """Sole SSH heartbeat channel; EOF revokes permission; actor/watchdog/post survive this process."""
    c,rx,_=radio.modules();common.emit(dict(type='bootstrap'))
    controller=dict(unit='h102-gr-iio-tx-'+uuid.uuid4().hex+'.service',pid=os.getpid())
    radio.save(directory/'controller.json',controller)
    rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=True)
    base=['/usr/bin/python3','-B',str(Path(__file__).absolute())]
    process=subprocess.Popen(['systemd-run','--unit='+controller['unit'],'--property=Type=notify',
        '--property=WatchdogSec=5','--property=FileDescriptorStoreMax=3','--property=TimeoutStartSec=90',
        '--property=TimeoutStopSec=45','--property=TimeoutAbortSec=2',
        '--property=Restart=no','--property=UMask=0077',
        '--property=ExecStopPost='+shlex.join(base+['post','--directory',str(directory)]),
        '--wait']+base+['run','--directory',str(directory)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    sent=False;eof=False;buffer=b''
    try:
        while process.poll() is None:
            if not sent and (directory/'ready.json').exists() and (directory/'health.json').exists():
                common.emit(dict(type='ready',ready=c.read_json(directory/'ready.json'),
                    health=c.read_json(directory/'health.json'),now_ns=time.monotonic_ns()));sent=True
            if not eof and select.select([sys.stdin.buffer],[],[],.1)[0]:
                chunk=os.read(sys.stdin.fileno(),8192)
                if not chunk:eof=True;(directory/'stop').touch(mode=0o600,exist_ok=True)
                else:
                    buffer+=chunk
                    if len(buffer)>65536:raise ValueError('IIO_TX_CONTROL_SIZE')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1);action=json.loads(line)['action']
                        if action=='pulse':
                            rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=False)
                            health=c.read_json(directory/'health.json') if (directory/'health.json').exists() else None
                            common.emit(dict(type='pulse',health=health,now_ns=time.monotonic_ns()))
                        elif action=='stop':(directory/'stop').touch(mode=0o600,exist_ok=True)
                        else:raise ValueError('IIO_TX_CONTROL_ACTION')
            elif eof:time.sleep(.1)
        out,err=process.communicate(timeout=2)
        result=dict(returncode=process.returncode,stdout=out.decode(errors='replace'),stderr=err.decode(errors='replace'),
            unit=controller['unit'],records={n:c.read_json(directory/n) for n in
                ('request.json','ready.json','main.json','post.json') if (directory/n).exists()})
        radio.save(directory/'manager.json',result);common.emit(dict(type='finished',result=result))
    finally:
        if process.poll() is None:(directory/'stop').touch(mode=0o600,exist_ok=True)


def warm(directory):
    """Reuse fixed-transport warmup; no hardware access or locks before start."""
    c,_,_=radio.modules();c.private(directory,directory=True)
    for name in SOURCES:c.private(directory/name)
    common.emit(dict(type='warm'));buffer=b'';last=time.monotonic()
    while time.monotonic()-last<15:
        if not select.select([sys.stdin.buffer],[],[],.1)[0]:continue
        chunk=os.read(sys.stdin.fileno(),8192)
        if not chunk:return
        buffer+=chunk
        if len(buffer)>65536:raise ValueError('IIO_TX_WARM_SIZE')
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1);value=json.loads(line);last=time.monotonic()
            if value['action']=='pulse':continue
            if value['action']=='stop':return
            if value['action']!='start' or buffer.strip():raise ValueError('IIO_TX_WARM_ACTION')
            validate(value['expected']);radio.save(directory/'expected.json',value['expected']);control(directory);return
    raise TimeoutError('IIO_TX_WARM_EXPIRED')


def main():
    """Root-private temporary entry maps actions to this module only, never the original RF actor."""
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('warm','run','post'))
    parser.add_argument('--directory',type=Path,required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise PermissionError('ROOT_REQUIRED')
    if args.directory.parent!=Path('/run') or not args.directory.name.startswith('h102-gui-session-'):
        raise ValueError('PRIVATE_RUN_DIRECTORY_REQUIRED')
    os.umask(0o077);{'warm':warm,'run':actor,'post':post}[args.action](args.directory)


if __name__=='__main__':main()
# End of file: board_iio_tx.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
