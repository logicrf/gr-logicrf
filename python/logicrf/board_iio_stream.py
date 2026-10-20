#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file board_iio_stream.py
@brief Exclusive RF permission, overflow windows and independent recovery for GNU Radio IIO mixed RX/TX.
@details Root temporary sessions reuse pinned hardware tools/SSH protocol; PID1 retains three locks through post.
         Allow selected DMA only; RF defaults off and requires explicit request; no cyclic transmitter.
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

from contextlib import ExitStack
import board_capture as capture
import board_tx as common
from board_tx import radio

SOURCES=('board_iio_stream.py','board_tx.py','board_radio.py','tx_cyclic.py','board_capture.py')


def validate(expected):
    """Independently check channels, finite lengths and budgets on-board beyond host admission checks."""
    channels=expected['channels'];samples=expected['samples'];rate=expected['rate']
    if channels not in ([],[1],[2],[1,2]) or type(samples) is not int or samples<0:
        raise ValueError('IIO_TX_CHANNELS_OR_LENGTH')
    if not isinstance(rate,(int,float)) or not math.isfinite(rate) or rate<=0 or rate*4*len(channels)>64_000_000:
        raise ValueError('IIO_TX_NETWORK_BUDGET')
    if samples/rate>300:raise ValueError('IIO_TX_FINITE_DURATION')
    selected=expected.get('rx_channels',[])
    if selected not in ([],[1],[2],[1,2]) or rate*4*len(selected)>64_000_000:
        raise ValueError('IIO_RX_CHANNELS_OR_BUDGET')
    if rate*4*(len(channels)+len(selected))>64_000_000:raise ValueError('IIO_STREAM_AGGREGATE_VALIDATED_BUDGET')
    if not channels and (not selected or expected.get('rf_enabled',False)):raise ValueError('IIO_STREAM_DIRECTION_OR_RF')
    if type(expected.get('rf_enabled',False)) is not bool:raise ValueError('RF_EXPLICIT_BOOLEAN_REQUIRED')
    for key in ('frequency','rx_frequency'):
        value=expected.get(key,expected['frequency'])
        if type(value) is not int or not 50_000_000<=value<=5_800_000_000:raise ValueError('RF_FREQUENCY_RANGE')
    attenuation=expected.get('attenuation_db',40)
    if not isinstance(attenuation,(int,float)) or not math.isfinite(attenuation) or not 20<=attenuation<=40 or abs(attenuation*20-round(attenuation*20))>1e-6:
        raise ValueError('TX_ATTENUATION_RANGE')
    gains=expected.get('rx_gain_db',{})
    if any(str(ch) not in ('1','2') or not isinstance(gain,(int,float)) or not math.isfinite(gain) or not -3<=gain<=30 for ch,gain in gains.items()):
        raise ValueError('RX_GAIN_RANGE')
    return math.ceil(samples/rate)+35 if samples else 0


def sources_off(hw,channels,rx_channels=(),rf_channels=()):
    """Allow registered-direction DMA and explicit RF routes only; DDS always stays off."""
    paths=[hw.phy/f'out_voltage{i}_en' for i in (0,1) if i+1 not in rf_channels]
    paths += [hw.alt(n,i,'raw') for n in common.DDS for i in range(4)]
    names=tuple(n for ch,n in enumerate(('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc'),1) if ch not in rx_channels)
    names+=tuple(n for ch,n in enumerate(common.DDS,1) if ch not in channels)
    paths += [hw.devices[n]/'buffer/enable' for n in names]
    values={str(p):hw.read(p) for p in paths}
    if any(v!='0' for v in values.values()):raise RuntimeError('IIO_STREAM_FOREIGN_SOURCE')
    return values


def configure(hw,expected,state,request,c,rx,directory):
    """Tune/set gain after locks and recovery request freeze; RF defaults off and no generator starts."""
    def check(calibrated=False):
        common.heartbeat_age(c.read_json(directory/'heartbeat.json'),time.monotonic_ns())
        common.check_store(request);hw.verify(complete=False)
        if calibrated:hw.calibration_idle()
        else:hw.idle();rx.stopped_tx(hw)
    mapping=radio.describe(hw,state,c.CATALOG)
    for group in mapping['groups']:
        if not set(group['channels']).intersection(expected['rx_channels']):continue
        if not group['editable']:raise ValueError('RX_LO_GROUP_NOT_EDITABLE')
        if any(mapping['rx_center_frequency_hz'][str(ch)]!=expected['rx_frequency'] for ch in group['channels']):
            radio.apply_carrier(hw,hw.settings(),group['channels'],expected['rx_frequency'],check,tx_channels=group['tx_shared'])
    mapping=radio.describe(hw,state,c.CATALOG)
    tx_los={mapping['mapping'][f'tx{ch}LoSelect'] for ch in expected['channels']}
    if any(mapping['rx_center_frequency_hz'][str(ch)]!=expected['frequency'] for ch in (1,2)
           if mapping['mapping'][f'rx{ch}LoSelect'] in tx_los):raise ValueError('RX_TX_SHARED_LO_FREQUENCY_MISMATCH')
    affected=[ch for ch in (1,2) if mapping['mapping'][f'tx{ch}LoSelect'] in tx_los]
    check()
    if not expected['rf_enabled']:common.apply_tx_carrier(hw,mapping['mapping'],affected,expected['frequency'])
    if expected['rf_enabled']:
        for i in (0,1):
            check();hw.write(hw.phy/f'out_voltage{i}_ensm_mode','calibrated')
            hw.write(hw.phy/f'out_voltage{i}_atten_control_mode','spi')
        for i in (0,1):
            hw.write(hw.phy/f'out_voltage{i}_en','1')
        common.apply_tx_carrier(hw,mapping['mapping'],affected,expected['frequency'])
        # TX power can reset RX gain indices; restore original/user gain before granting data permission.
        for i in (0,1):
            hw.write(hw.phy/f'in_voltage{i}_ensm_mode','rf_enabled')
            hw.write(hw.phy/f'in_voltage{i}_gain_control_mode','spi')
            path=hw.phy/f'in_voltage{i}_hardwaregain'
            gain=expected['rx_gain_db'].get(str(i+1),request['settings'][str(path)].split()[0])
            hw.write(path,str(gain),.001)
            for name in ('gain_control_mode','ensm_mode'):
                path=hw.phy/f'in_voltage{i}_{name}'
                if str(i+1) not in expected['rx_gain_db']:hw.write(path,request['settings'][str(path)])
        for i in (0,1):hw.write(hw.phy/f'out_voltage{i}_ensm_mode','primed')
        for ch in expected['channels']:
            hw.write(hw.phy/f'out_voltage{ch-1}_ensm_mode','rf_enabled')
            hw.write(hw.phy/f'out_voltage{ch-1}_hardwaregain','-20' if expected['attenuation_db']>30 else '-40',.001)
            hw.write(hw.phy/f'out_voltage{ch-1}_hardwaregain',str(-expected['attenuation_db']),.001)
    else:
        for ch in expected['channels']:
            hw.write(hw.phy/f'out_voltage{ch-1}_atten_control_mode','spi')
            hw.write(hw.phy/f'out_voltage{ch-1}_hardwaregain',str(-expected['attenuation_db']),.001)
        for ch,gain in expected['rx_gain_db'].items():
            hw.write(hw.phy/f'in_voltage{int(ch)-1}_gain_control_mode','spi')
            hw.write(hw.phy/f'in_voltage{int(ch)-1}_hardwaregain',str(gain),.001)
    active=hw.settings();radio.save(directory/'active-settings.json',active)
    return active,radio.describe(hw,state,c.CATALOG)


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
    controller=c.read_json(directory/'controller.json');files=[];hw=None;request=None;guard=None;stack=ExitStack()
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
        if any(params[ch]['sample_rate_hz']!=expected['rate'] for ch in channels) or any(
               p['sample_rate_hz']!=expected['rate'] for p in actual['parameters']['rx'] if p['channel'] in expected['rx_channels']):
            raise ValueError('IIO_TX_RATE_OR_FREQUENCY_CHANGED')
        import h102_iio_tx_lease as dma
        request=dict(unit=controller['unit'],invocation=os.environ['INVOCATION_ID'],identity=identity,
            actual=actual,configuration_id=state['configuration_id'],channels=channels,settings=hw.settings(),
            dma_settings=dma.dma_settings(hw),ssi_modes=rx.ssi_modes(hw),
            tx_modes={str(i):hw.read(hw.phy/f'out_voltage{i}_ensm_mode') for i in (0,1)},
            sources={n:hashlib.sha256((directory/n).read_bytes()).hexdigest() for n in SOURCES})
        common.notify('FDSTORE=1\nFDPOLL=0\nFDNAME=h102-board-locks',[f.fileno() for f in files])
        until=time.monotonic()+3
        while common.unit_status(request['unit'])['NFileDescriptorStore']!='3' and time.monotonic()<until:time.sleep(.05)
        common.check_store(request);radio.save(directory/'request.json',request)
        active,active_radio=configure(hw,expected,state,request,c,rx,directory)
        issued=time.monotonic_ns();ready=dict(pid=os.getpid(),issued_ns=issued,deadline_ns=issued+seconds*10**9 if seconds else 0,
            identity=identity,configuration_id=state['configuration_id'],RF_TX=expected['rf_enabled'],channels=channels,radio=active_radio,
            rx_gain_readback={str(i+1):hw.read(hw.phy/f'in_voltage{i}_hardwaregain') for i in (0,1)},
            tx_gain_readback={str(i+1):hw.read(hw.phy/f'out_voltage{i}_hardwaregain') for i in (0,1)})
        radio.save(directory/'ready.json',ready);signal.alarm(seconds+2 if seconds else 0)
        next_check=0;last_command=0;active_modes={str(i):hw.read(hw.phy/f'out_voltage{i}_ensm_mode') for i in (0,1)};active_power={str(i):hw.read(hw.phy/f'out_voltage{i}_en') for i in (0,1)}
        while not (directory/'stop').exists():
            heartbeat=c.read_json(directory/'heartbeat.json');now=time.monotonic_ns();common.heartbeat_age(heartbeat,now)
            if ready['deadline_ns'] and now>=ready['deadline_ns']:raise TimeoutError('IIO_TX_TASK_DEADLINE')
            if (directory/'command.json').exists():
                command=c.read_json(directory/'command.json')
                if command['sequence']>last_command:
                    last_command=command['sequence'];action=command['action'];ch=command['channel']
                    if action in ('guard_begin','guard_arm','guard_end'):
                        if ch not in expected['rx_channels']:raise ValueError('GUARD_RX_NOT_SELECTED')
                        if guard is None:guard=stack.enter_context(capture.OverflowGuard(hw,identity))
                        result=getattr(guard,action[6:])(ch)
                        rx.atomic_save(directory/f'guard-rx{ch}.json',result,exclusive=False)
                    elif action=='tx_end':
                        if ch not in channels:raise ValueError('TX_END_NOT_SELECTED')
                        if expected['rf_enabled']:hw.write(hw.phy/f'out_voltage{ch-1}_ensm_mode','primed')
                        active_modes[str(ch-1)]=hw.read(hw.phy/f'out_voltage{ch-1}_ensm_mode')
                        active=hw.settings();result=dict(channel=ch,RF_TX=False)
                    else:raise ValueError('IIO_STREAM_NATIVE_ACTION')
                    rx.atomic_save(directory/'reply.json',dict(sequence=last_command,permitted=True,guard=result),exclusive=False)
            if guard is not None:guard.poll()
            if time.monotonic()>=next_check:
                common.check_store(request);hw.verify(complete=False);sources_off(hw,channels,expected['rx_channels'],(1,2) if expected['rf_enabled'] else ())
                if c.snapshot(hw)!=actual or rx.ssi_modes(hw)!=request['ssi_modes']:
                    raise RuntimeError('IIO_TX_ACTIVE_CONFIGURATION_CHANGED')
                # Keep non-enable settings unchanged; sources_off and target-buffer readback independently verify source enables.
                if any(hw.read(hw.phy/f'out_voltage{i}_ensm_mode')!=value for i,value in active_modes.items()):raise RuntimeError('TX_RF_MODE_CHANGED')
                if any(hw.read(hw.phy/f'out_voltage{i}_en')!=value for i,value in active_power.items()):raise RuntimeError('TX_RF_POWER_CHANGED')
                current=hw.settings();baseline=dict(active)
                for name in tuple(common.DDS[ch-1] for ch in channels)+tuple(
                    ('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc')[ch-1] for ch in expected['rx_channels']):
                    key=str(hw.devices[name]/'buffer/enable');current.pop(key,None);baseline.pop(key,None)
                if current!=baseline:raise RuntimeError('IIO_TX_ACTIVE_SETTINGS_CHANGED')
                rx.atomic_save(directory/'health.json',dict(checked_ns=now,permitted=True,
                    target_buffers={str(ch):hw.read(hw.devices[common.DDS[ch-1]]/'buffer/enable') for ch in channels}),exclusive=False)
                next_check=time.monotonic()+.5
            common.notify('READY=1\nWATCHDOG=1');time.sleep(.1)
        radio.save(directory/'main.json',dict(status='stopped',RF_TX=expected['rf_enabled']))
    except Exception as error:
        if not (directory/'main.json').exists():radio.save(directory/'main.json',dict(status='failed',error=str(error)))
        raise
    finally:
        signal.alarm(0)
        # Power down within the actor still holding verified identity/three locks; full post hashing must not delay RF shutdown.
        if hw is not None and (directory/'request.json').exists():
            stop_errors=hw.stop()
            if stop_errors:radio.save(directory/'rf-immediate-stop-error.json',dict(errors=stop_errors))
        stack.close()
        for f in files:f.close()  # PID1 retains the same OFDs through independent post; never LOCK_UN.


def post(directory):
    """Stop sources/restore DMA while PID1 retains three locks; preserve original errors."""
    if not (directory/'request.json').exists():return
    c,rx,request=load(directory);common.check_store(request,post=True)
    if c.identity()!=request['identity']:raise ValueError('IIO_TX_POST_IDENTITY')
    hw=c.rf.Hardware(directory,request['identity'],None);errors=hw.stop()
    import h102_iio_tx_lease as dma
    restored=dma.restore_dma(hw,request['dma_settings']);errors+=restored['errors']
    errors+=common.settings_restore(hw,request['settings'],request['tx_modes'])
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
    controller=dict(unit='h102-gr-iio-stream-'+uuid.uuid4().hex+'.service',pid=os.getpid())
    radio.save(directory/'controller.json',controller)
    rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=True)
    base=['/usr/bin/python3','-B',str(Path(__file__).absolute())]
    process=subprocess.Popen(['systemd-run','--unit='+controller['unit'],'--property=Type=notify',
        '--property=WatchdogSec=5','--property=FileDescriptorStoreMax=3','--property=TimeoutStartSec=90',
        '--property=TimeoutStopSec=45','--property=TimeoutAbortSec=2',
        '--property=Restart=no','--property=UMask=0077',
        '--property=ExecStopPost='+shlex.join(base+['post','--directory',str(directory)]),
        '--wait']+base+['run','--directory',str(directory)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    sent=False;eof=False;buffer=b'';sequence=0;pending=None
    try:
        while process.poll() is None:
            if not sent and (directory/'ready.json').exists() and (directory/'health.json').exists():
                common.emit(dict(type='ready',ready=c.read_json(directory/'ready.json'),
                    health=c.read_json(directory/'health.json'),now_ns=time.monotonic_ns()));sent=True
            if pending and (directory/'reply.json').exists():
                reply=c.read_json(directory/'reply.json')
                if reply['sequence']==sequence:common.emit(dict(type='reply',id=pending,result=reply));pending=None
            if not eof and select.select([sys.stdin.buffer],[],[],.1)[0]:
                chunk=os.read(sys.stdin.fileno(),8192)
                if not chunk:eof=True;(directory/'stop').touch(mode=0o600,exist_ok=True)
                else:
                    buffer+=chunk
                    if len(buffer)>65536:raise ValueError('IIO_TX_CONTROL_SIZE')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1);value=json.loads(line);action=value['action']
                        if action=='pulse':
                            rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=False)
                            health=c.read_json(directory/'health.json') if (directory/'health.json').exists() else None
                            common.emit(dict(type='pulse',health=health,now_ns=time.monotonic_ns()))
                        elif action=='stop':(directory/'stop').touch(mode=0o600,exist_ok=True)
                        elif action in ('guard_begin','guard_arm','guard_end','tx_end'):
                            if pending:raise RuntimeError('IIO_STREAM_CONTROL_PENDING')
                            sequence+=1;pending=value['id']
                            rx.atomic_save(directory/'command.json',dict(sequence=sequence,action=action,channel=value['channel']),exclusive=False)
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
# End of file: board_iio_stream.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
