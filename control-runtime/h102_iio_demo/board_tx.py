#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       board_tx.py
@brief      Independent finite TX owner with local cyclic IQ, heartbeat deadline and main/post lock custody.
@details    PID1 retains the original three flock descriptors until ExecStopPost finishes; no USB operation.
            Independent LO permits TX tuning; shared LO requires actual RX frequency agreement. post disables RF first.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.6.0
@date       2026-10-15
"""
import argparse
import array
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

if __package__:
    from . import board_radio as radio
else:
    import board_radio as radio

TTL = 15
SOURCES = ('board_tx.py', 'board_radio.py', 'tx_cyclic.py')
LOCKS = ('h102-profile-config.lock', 'h102-rx-session.lock', 'h102-gnss-g0.lock')
DDS = ('axi-adrv9002-tx-lpc', 'axi-adrv9002-tx2-lpc')


def notify(message, descriptors=()):
    """Native main sends status or original flock SCM_RIGHTS through systemd's notification socket."""
    address = os.environ['NOTIFY_SOCKET']
    if address.startswith('@'): address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wire:
        wire.connect(address)
        rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors))] if descriptors else []
        wire.sendmsg([message.encode()], rights)


def unit_status(unit):
    """Read actual native Invocation, main/post PID and FDSTORE count."""
    output = subprocess.check_output(['systemctl', 'show', unit, '-p', 'InvocationID', '-p', 'MainPID',
        '-p', 'ControlPID', '-p', 'NFileDescriptorStore', '-p', 'ActiveState'], text=True, timeout=2)
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def check_store(request, post=False):
    """Allow hardware access only for the same Invocation, execution process and three locks held by PID1."""
    actual = unit_status(request['unit'])
    if (actual['InvocationID'] != request['invocation'] or request['invocation'] != os.environ.get('INVOCATION_ID')
            or actual['NFileDescriptorStore'] != '3'
            or actual['ControlPID' if post else 'MainPID'] != str(os.getpid())):
        raise RuntimeError('TX_NATIVE_LOCK_STORE_OR_INVOCATION_CHANGED')


def raw_validate(raw, expected):
    """Independently verify exact IQ layout, hash and complex amplitude on-board; do not trust GUI checks."""
    if len(raw) != 262144 or hashlib.sha256(raw).hexdigest() != expected['sha256']:
        raise ValueError('TX_WAVEFORM_LENGTH_OR_HASH')
    maximum = max(i*i+q*q for i, q in struct.iter_unpack('<hh', raw))
    if not 0 < maximum <= 8192**2: raise ValueError('TX_WAVEFORM_PEAK')


def validate(plan, mapping, parameters, rx_frequencies=None):
    """Verify finite TX and actual clocks; shared LO requires all shared RX readbacks to agree, without enabling hardware."""
    if plan['channels'] not in ([1], [2], [1, 2]): raise ValueError('TX_CHANNELS')
    if plan.get('calibrate',False):raise ValueError('TX_EXPERIMENTAL_CALIBRATION_NOT_SUPPORTED')
    if type(plan['seconds']) is not int or not 1 <= plan['seconds'] <= 300: raise ValueError('TX_DURATION')
    if type(plan['frequency_hz']) is not int or not 50_000_000 <= plan['frequency_hz'] <= 5_800_000_000:
        raise ValueError('TX_FREQUENCY')
    gain = plan['attenuation_db']
    if not math.isfinite(gain) or not 20 <= gain <= 40 or abs(gain*20-round(gain*20)) > 1e-6:
        raise ValueError('TX_ATTENUATION')
    tx_los = {mapping[f'tx{c}LoSelect'] for c in plan['channels']}
    for channel in (1, 2):
        if mapping[f'rx{channel}LoSelect'] in tx_los:
            frequency = (rx_frequencies or {}).get(str(channel))
            if type(frequency) is not int or frequency != plan['frequency_hz']:
                raise ValueError('RX_TX_SHARED_LO_FREQUENCY_MISMATCH')
    for channel in plan['channels']:
        wave = plan['waveforms'][str(channel)]
        actual = next(p for p in parameters['tx'] if p['channel'] == channel)
        if any(wave[k] != actual[k] for k in ('sample_rate_hz', 'rf_bandwidth_hz')):
            raise ValueError('TX_WAVEFORM_CLOCK_CHANGED')
    return [c for c in (1, 2) if mapping[f'tx{c}LoSelect'] in tx_los]


def apply_tx_carrier(hw, mapping, affected, frequency):
    """Tune independent TX LO only; reject shared-LO drift after start and preserve shared RX frequency."""
    paths={channel:hw.alt('adrv9002-phy',channel+1,'frequency') for channel in affected}
    current={channel:int(hw.read(path)) for channel,path in paths.items()}
    for channel in affected:
        lo=mapping[f'tx{channel}LoSelect']
        if current[channel]!=frequency and any(mapping[f'rx{c}LoSelect']==lo for c in (1,2)):
            raise RuntimeError('TX_SHARED_RX_LO_CHANGED')
    for channel,path in paths.items():
        # The first TX write changes its peer in the group; reread each port and avoid redundant independent-LO tuning.
        if int(hw.read(path))!=frequency:hw.write(path,str(frequency))
    if any(int(hw.read(path))!=frequency for path in paths.values()):
        raise RuntimeError('TX_SHARED_LO_READBACK')


def load(directory):
    """Verify root-private frozen request and complete TX source set; reject any drift before writes."""
    c, rx, _ = radio.modules()
    c.private(directory, directory=True); c.private(directory/'request.json')
    request = c.read_json(directory/'request.json')
    if set(request['sources']) != set(SOURCES): raise ValueError('TX_SOURCE_SET')
    for name, digest in request['sources'].items():
        c.private(directory/name)
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != digest: raise ValueError('TX_SOURCE_CHANGED')
    return c, rx, request


def heartbeat_age(value, now_ns):
    """Host input refreshes board-monotonic liveness; future/expired heartbeats are invalid."""
    age = (now_ns-value['updated_ns'])/1e9
    if not 0 <= age < TTL: raise TimeoutError('HOST_HEARTBEAT_EXPIRED')
    return age


def settings_restore(hw, before, tx_modes):
    """After RF shutdown restore LO/RX and cached TX mode; do not re-enable RF to repair gain."""
    hw.write(hw.phy/'initial_calibrations', 'off')
    errors = []
    for path, value in reversed(list(before.items())):
        p = Path(path)
        try:
            if hw.read(p) == value: continue
            if p.name.startswith('in_voltage') and p.name.endswith('_hardwaregain'):
                index = p.name[len('in_voltage')]
                hw.write(hw.phy/f'in_voltage{index}_ensm_mode', 'rf_enabled')
                hw.write(hw.phy/f'in_voltage{index}_gain_control_mode', 'spi')
            hw.write(p, value.split()[0] if p.name.endswith('_hardwaregain') else value,
                     .001 if p.name.endswith(('_hardwaregain', '_scale')) else None)
        except Exception as error: errors.append(str(error))
    for i, value in tx_modes.items():
        try: hw.write(hw.phy/f'out_voltage{i}_ensm_mode', value)
        except Exception as error: errors.append(str(error))
    return errors


def post(directory):
    """Independent recovery retains PID1's three locks; stop RF before hashing large files or restoring settings."""
    if not (directory/'request.json').exists(): return  # No hardware writes before request persistence.
    c, rx, request = load(directory); check_store(request, post=True)
    if c.identity() != request['identity']: raise ValueError('TX_POST_IDENTITY_CHANGED')
    # Hardware construction hashes boot inputs, so disable RF first through the request's pinned PHY path.
    phy = Path(request['phy'])
    if phy.parent != Path('/sys/bus/iio/devices') or not phy.name.startswith('iio:device'):
        raise ValueError('TX_PHY_PATH')
    early_errors = []
    for i in (0, 1):
        try:
            p = phy/f'out_voltage{i}_en'; p.write_text('0\n')
            if p.read_text().strip() != '0': raise RuntimeError('RF_OFF_READBACK')
        except Exception as error: early_errors.append(str(error))
    hw = c.rf.Hardware(directory, request['identity'], None)
    errors = early_errors + hw.stop()
    import h102_iio_tx_lease as tx_dma
    dma_restore=tx_dma.restore_dma(hw,request['dma_settings'])
    errors+=dma_restore['errors']
    errors += settings_restore(hw, request['settings'], request['tx_modes'])
    errors += hw.stop()
    try:
        buffers = rx.restore_network_kernel_buffers()
        hw.idle()
    except Exception as error: errors.append(str(error)); buffers = None
    state = c.read_json(c.STATE)
    result = dict(passed=not errors, errors=errors, rf_off=all(hw.read(hw.phy/f'out_voltage{i}_en')=='0' for i in (0,1)),
        settings_restored=hw.settings()==request['settings'], configuration_unchanged=c.snapshot(hw)==request['actual'],
        radio=radio.describe(hw,state,c.CATALOG), dma_restore=dma_restore,
        network_kernel_buffers=buffers, lock_store=unit_status(request['unit']))
    result['passed'] = result['passed'] and result['settings_restored'] and result['configuration_unchanged'] and result['rf_off']
    radio.save(directory/'post.json',result)
    if not result['passed']: raise RuntimeError('TX_POST_RECOVERY_FAILED')


def actor(directory):
    """Finite TX: escrow locks, preload waveform, start RF, enforce deadline and run independent post."""
    c, rx, _ = radio.modules(); c.private(directory,directory=True); c.private(directory/'expected.json')
    expected = c.read_json(directory/'expected.json'); info = expected['info']; plan = expected['tx']
    controller = c.read_json(directory/'controller.json'); files = []
    client = None; hw = None; request = None; ready = None; active = False
    def interrupted(signum, frame): raise TimeoutError('TX_NATIVE_INTERRUPTED')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(85)
    try:
        for name in LOCKS:
            f = open('/run/lock/'+name,'a+b'); files.append(f)
            until=time.monotonic()+3
            while True:
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if name!=LOCKS[0] or time.monotonic()>=until:raise RuntimeError('TX_DEVICE_BUSY_NO_WRITES')
                    time.sleep(.02)
        identity = c.identity(); hw = c.rf.Hardware(directory,identity,None); hw.idle(); rx.stopped_tx(hw)
        if (Path('/sys/kernel/debug/iio')/hw.phy.name/'api_version').read_text().strip()!=c.API:
            raise ValueError('TX_DRIVER_API_CHANGED')
        state = c.read_json(c.STATE); actual = c.snapshot(hw)
        package = c.load_package(c.CATALOG,state['profile_id'],trusted=True); c.verify_snapshot(actual,package)
        mapping = radio.describe(hw,state,c.CATALOG)
        if (identity!=state['identity'] or actual!=state['actual'] or state['sha256']!=package['manifest']['sha256']
                or info['boot_id']!=identity['boot_id'] or info['configuration_id']!=state['configuration_id']
                or info['active']!=state['profile_id'] or info['parameters']!=actual['parameters'] or info['radio']!=mapping):
            raise ValueError('TX_EXPECTED_CONFIGURATION_CHANGED')
        affected = validate(plan,mapping['mapping'],actual['parameters'],mapping['rx_center_frequency_hz'])
        if set(rx.ssi_modes(hw).values())!={'TESTMODE_DATA_NORMAL'}: raise ValueError('TX_FOREIGN_SSI_MODE')
        raws = {channel:base64.b64decode(expected['raw'][str(channel)],validate=True) for channel in plan['channels']}
        for channel, raw in raws.items(): raw_validate(raw,plan['waveforms'][str(channel)])
        import h102_iio_tx_lease as tx_dma
        request = dict(unit=controller['unit'],invocation=os.environ['INVOCATION_ID'],identity=identity,actual=actual,
            configuration_id=state['configuration_id'],profile_id=state['profile_id'],phy=str(hw.phy),settings=hw.settings(),
            tx_modes={str(i):hw.read(hw.phy/f'out_voltage{i}_ensm_mode') for i in (0,1)},tx=plan,dma_settings=tx_dma.dma_settings(hw),
            sources={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in SOURCES})
        notify('FDSTORE=1\nFDPOLL=0\nFDNAME=h102-board-locks',[f.fileno() for f in files])
        until=time.monotonic()+3
        while unit_status(request['unit'])['NFileDescriptorStore']!='3' and time.monotonic()<until: time.sleep(.05)
        check_store(request);radio.save(directory/'request.json',request)
        # Attribute changes begin only after source identity and lock-custody proofs; otherwise no enable path is reached.
        if hw.stop(): raise RuntimeError('TX_INITIAL_STOP')
        for i in (0,1):
            hw.write(hw.phy/f'out_voltage{i}_ensm_mode','calibrated')
            hw.write(hw.phy/f'out_voltage{i}_atten_control_mode','spi')
            hw.write(hw.phy/f'out_voltage{i}_en','1')
        apply_tx_carrier(hw,mapping['mapping'],affected,plan['frequency_hz'])
        # Initial calibration resets active RX gain index; restore pre-session readback before comparing RF amplitude.
        for i in (0,1):
            hw.write(hw.phy/f'in_voltage{i}_ensm_mode','rf_enabled')
            hw.write(hw.phy/f'in_voltage{i}_gain_control_mode','spi')
            p=hw.phy/f'in_voltage{i}_hardwaregain';hw.write(p,request['settings'][str(p)].split()[0],.001)
            for name in ('gain_control_mode','ensm_mode'):
                p=hw.phy/f'in_voltage{i}_{name}'
                if hw.read(p)!=request['settings'][str(p)]:hw.write(p,request['settings'][str(p)])
        for i in (0,1):
            hw.write(hw.phy/f'out_voltage{i}_ensm_mode','primed')
            # Keep the shared-LO peer TX powered in PRIMED; only selected channels enter RF_ENABLED.
            # Powering down the peer alone changes shared circuitry; both channels still power down at stop.
        from tx_cyclic import CyclicTX
        def permit():
            heartbeat_age(c.read_json(directory/'heartbeat.json'),time.monotonic_ns());hw.verify(complete=False)
            return not (directory/'stop').exists()
        permit()
        client=CyclicTX('local:',permit)
        # Cross analog attenuation ranges during zero IQ before applying the target. Rewriting the same HP range cannot
        # remove this board's roughly 32 dB startup offset. Submit user IQ only after successful readback.
        priming={str(ch):client.transmit(ch,bytes(len(raw))) for ch,raw in raws.items()}
        check_store(request);permit()
        active=True;started=time.monotonic();deadline=started+plan['seconds']
        for channel in plan['channels']:
            hw.write(hw.phy/f'out_voltage{channel-1}_ensm_mode','rf_enabled')
            hw.write(hw.phy/f'out_voltage{channel-1}_hardwaregain',
                     '-40' if plan['attenuation_db']<=30 else '-20',.001)
            hw.write(hw.phy/f'out_voltage{channel-1}_hardwaregain',str(-plan['attenuation_db']),.001)
        client.close();client=None
        client=CyclicTX('local:',permit)
        pushed={str(ch):client.transmit(ch,raw) for ch,raw in raws.items()}
        gains={str(ch):hw.read(hw.phy/f'out_voltage{ch-1}_hardwaregain') for ch in plan['channels']}
        ready=dict(pid=os.getpid(),configuration_id=request['configuration_id'],identity=identity,mode='tx',
            issued_ns=time.monotonic_ns(),heartbeat_ttl_seconds=TTL,tx=plan,pushed=pushed,gain_readback=gains,
            zero_iq_priming=priming,
            deadline_monotonic=deadline,radio=radio.describe(hw,state,c.CATALOG),
            tx_states={str(i+1):dict(power=hw.read(hw.phy/f'out_voltage{i}_en'),
                       ensm=hw.read(hw.phy/f'out_voltage{i}_ensm_mode')) for i in (0,1)},
            rx_gain_readback={str(i+1):hw.read(hw.phy/f'in_voltage{i}_hardwaregain') for i in (0,1)})
        radio.save(directory/'ready.json',ready);signal.alarm(0);notify('READY=1\nWATCHDOG=1')
        reason='stopped'
        while not (directory/'stop').exists():
            # Read heartbeat timestamp before sampling now to avoid treating a concurrent renewal as a future heartbeat.
            heartbeat=c.read_json(directory/'heartbeat.json');now=time.monotonic_ns()
            heartbeat_age(heartbeat,now)
            hw.verify(complete=False)
            if time.monotonic()>=deadline: reason='deadline';break
            rx.atomic_save(directory/'health.json',dict(checked_ns=now,permitted=True,ready=ready),exclusive=False)
            notify('WATCHDOG=1');time.sleep(.2)
        # Disable RF at the deadline; hold locks until host RX buffers close so post cannot race active RX.
        if client is not None: client.close();client=None
        for ch in plan['channels']:
            p=hw.phy/f'out_voltage{ch-1}_hardwaregain';hw.write(p,request['settings'][str(p)].split()[0],.001)
        stop_errors=hw.stop();active=False
        if stop_errors:raise RuntimeError('TX_STOP_FAILED: '+str(stop_errors))
        radio.save(directory/'tx-off.json',dict(reason=reason,rf_seconds=time.monotonic()-started))
        drain_until=time.monotonic()+TTL
        while not (directory/'stop').exists() and time.monotonic()<drain_until:
            heartbeat=c.read_json(directory/'heartbeat.json');now=time.monotonic_ns()
            heartbeat_age(heartbeat,now)
            rx.atomic_save(directory/'health.json',dict(checked_ns=now,permitted=True,rf_off=True),exclusive=False)
            notify('WATCHDOG=1');time.sleep(.2)
        radio.save(directory/'main.json',dict(status=reason,rf_seconds=c.read_json(directory/'tx-off.json')['rf_seconds']))
    except Exception as error:
        if not (directory/'main.json').exists():radio.save(directory/'main.json',dict(status='failed',error=str(error)))
        raise
    finally:
        signal.alarm(0)
        if hw is not None and request is not None and (directory/'request.json').exists():
            # Normal stop removes the waveform, restores attenuation while active, then powers down; attempt all shutdowns on failure.
            cleanup_errors=[]
            if client is not None:
                try:client.close()
                except Exception as error:cleanup_errors.append(str(error))
                client=None
            if active:
                for ch in plan['channels']:
                    try:
                        p=hw.phy/f'out_voltage{ch-1}_hardwaregain';hw.write(p,request['settings'][str(p)].split()[0],.001)
                    except Exception as error:cleanup_errors.append(str(error))
            cleanup_errors+=hw.stop()
            radio.save(directory/'main-stop.json',dict(errors=cleanup_errors,rf_off=not cleanup_errors))
        for f in files:f.close()  # PID1 retains the same OFD locks; do not LOCK_UN before post completes.


def emit(value):
    """Emit one bounded JSON response; do not accumulate raw data or unlimited health logs over SSH."""
    print(json.dumps(value),flush=True)


def control(directory):
    """Receive host heartbeat/stop on SSH stdin; EOF revokes permission without self-renewal."""
    started=time.monotonic()
    c,rx,_=radio.modules();c.private(directory,directory=True)
    emit(dict(type='bootstrap'))
    request=dict(unit='h102-gui-session-'+uuid.uuid4().hex+'.service',pid=os.getpid(),started_monotonic=started)
    radio.save(directory/'controller.json',request)
    rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=True)
    base=['/usr/bin/python3','-B',str(Path(__file__).absolute())]
    process=subprocess.Popen(['systemd-run','--unit='+request['unit'],'--property=Type=notify',
        '--property=WatchdogSec=5','--property=FileDescriptorStoreMax=3','--property=TimeoutStartSec=90','--property=TimeoutStopSec=45','--property=TimeoutAbortSec=2',
        '--property=Restart=no','--property=UMask=0077','--property=ExecStopPost='+shlex.join(base+['post','--directory',str(directory)]),
        '--wait']+base+['run','--directory',str(directory)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    sent_ready=False;sequence=0;pending=None;eof=False;buffer=b''
    try:
        while process.poll() is None:
            if not sent_ready and (directory/'ready.json').exists() and (directory/'health.json').exists():
                emit(dict(type='ready',ready=c.read_json(directory/'ready.json'),
                          health=c.read_json(directory/'health.json'),now_ns=time.monotonic_ns()));sent_ready=True
            if pending and (directory/'reply.json').exists():
                reply=c.read_json(directory/'reply.json')
                if reply['sequence']==sequence:
                    emit(dict(type='reply',id=pending,result=reply));pending=None
            if not eof and select.select([sys.stdin.buffer],[],[],.1)[0]:
                chunk=os.read(sys.stdin.fileno(),8192)
                if not chunk:
                    eof=True;(directory/'stop').touch(mode=0o600,exist_ok=True)
                else:
                    buffer+=chunk
                    if len(buffer)>1048576:raise ValueError('CONTROL_LINE_TOO_LARGE')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1);value=json.loads(line)
                        action=value['action']
                        if action=='pulse':
                            rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=False)
                            health=c.read_json(directory/'health.json') if (directory/'health.json').exists() else None
                            emit(dict(type='pulse',health=health,now_ns=time.monotonic_ns()))
                        elif action=='stop':(directory/'stop').touch(mode=0o600,exist_ok=True)
                        else:raise ValueError('CONTROL_ACTION')
            elif eof:time.sleep(.1)
        out,err=process.communicate(timeout=2)
        records={n:c.read_json(directory/n) for n in ('request.json','ready.json','main.json','post.json','main-stop.json') if (directory/n).exists()}
        result=dict(returncode=process.returncode,stdout=out.decode(errors='replace'),stderr=err.decode(errors='replace'),
                    unit=request['unit'],records=records)
        radio.save(directory/'manager.json',result);emit(dict(type='finished',result=result))
    finally:
        # Manager exit stops renewals; independent actor TTL/watchdog/post remain effective.
        if process.poll() is None:(directory/'stop').touch(mode=0o600,exist_ok=True)


def warm(directory):
    """Load private modules and wait for start without board locks, permissions or hardware access.

    Lines are bounded to 1 MiB for preloaded IQ. Exit after 15 s without input or on EOF; pulse keeps the channel only.
    start establishes expected identity and the native owner's independent heartbeat/recovery duties.
    """
    c,_,_=radio.modules();c.private(directory,directory=True)
    for name in SOURCES:c.private(directory/name)
    emit(dict(type='warm'))
    buffer=b'';last=time.monotonic()
    while time.monotonic()-last<15:
        if not select.select([sys.stdin.buffer],[],[],.1)[0]:continue
        chunk=os.read(sys.stdin.fileno(),8192)
        if not chunk:return
        buffer+=chunk
        if len(buffer)>1048576:raise ValueError('WARM_LINE_TOO_LARGE')
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1);value=json.loads(line);last=time.monotonic()
            if value['action']=='pulse':continue
            if value['action']=='stop':return
            if value['action']!='start':raise ValueError('WARM_ACTION')
            if buffer.strip():raise ValueError('WARM_PIPELINED_START')
            radio.save(directory/'expected.json',value['expected'])
            control(directory);return
    raise TimeoutError('WARM_TRANSPORT_EXPIRED')



def main():
    """Root-private session entry; actual TX runs independently of the RX-only actor."""
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['warm','control','run','post'])
    parser.add_argument('--directory',type=Path,required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise PermissionError('ROOT_REQUIRED')
    if args.directory.parent!=Path('/run') or not args.directory.name.startswith('h102-gui-session-'):
        raise ValueError('PRIVATE_RUN_DIRECTORY_REQUIRED')
    os.umask(0o077)
    {'warm':warm,'control':control,'run':actor,'post':post}[args.action](args.directory)


if __name__=='__main__':main()

# End of file: board_tx.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
