#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       board_session.py
@brief      Manage native live RX/sweep ownership and independent recovery using host heartbeats.
@details    Root-private /run module; main holds three locks; post rechecks identity and restores original RX/LO.
            idle_tune reuses control only, without IQ buffers or network buffer-count changes.
            Heartbeat TTL 15 s, watchdog 60 s; supervised off tuning/cleanup 35 s and auto tuning 180 s.
            The control protocol handles pulse/tune/stop only; never enable TX/DDS or modify the installed image.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.8.0
@date       2026-10-15
"""
from contextlib import ExitStack, contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import signal
import stat
import socket
import subprocess
import sys
import time
from threading import Event, Thread
import uuid

if __package__:
    from . import board_radio as radio
    from . import board_capture as capture
else:
    import board_radio as radio
    import board_capture as capture

TTL = 15


def complete_boot_checks(expected):
    """Read all startup files with two threads and verify full hashes without caching or skipped checks.

    Use only during native main/post preparation, before data buffers exist or after data collection ends.
    Concurrency is bounded by the board's two CPUs; collect every result before continuing.
    """
    def digest(item):
        path,wanted=item
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()==wanted
    with ThreadPoolExecutor(max_workers=2) as pool:
        checks=list(pool.map(digest,expected['boot_pins'].items()))
    if not checks or not all(checks):raise RuntimeError('BOOT_PIN')


def session_hardware_context(stack,config,directory,expected):
    """Preserve SDK1 locks/API/discovery; check identity before and after parallel full hashing.

    A private GUI actor subclass leaves released modules, global Hardware and the image unchanged.
    The caller takes the configuration lock first; RX/GNSS lock ordering matches the SDK1 factory.
    """
    class VerifiedHardware(config.rf.Hardware):
        """Inherit hardware operations; parallelize full hashing only, without RF changes."""
        def verify(self,complete=True):
            """Full validation for main/post; preserve lightweight identity checks during operation."""
            if self.fixture:return super().verify(complete)
            super().verify(complete=False)
            if complete:
                complete_boot_checks(self.expected)
                super().verify(complete=False)
    for name in ('h102-rx-session.lock','h102-gnss-g0.lock'):
        descriptor=stack.enter_context(open('/run/lock/'+name,'a+b'))
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    hw=VerifiedHardware(directory,expected,None)
    api=Path('/sys/kernel/debug/iio')/hw.phy.name/'api_version'
    if api.read_text().strip()!=config.API:raise ValueError('ACTUAL_DRIVER_API_MISMATCH')
    return hw


def load(directory):
    """Verify root directory, request and module hashes; reject modified control programs."""
    c,rx,lock=radio.modules()
    c.private(directory,directory=True);c.private(directory/'request.json')
    request=c.read_json(directory/'request.json')
    for name,digest in request['sources'].items():
        c.private(directory/name)
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('SESSION_SOURCE_CHANGED')
    return c,rx,lock,request


def locked(stack,directory,request):
    """Each main/post holds configuration/RX/GNSS locks and rechecks boot/API/profile and TX silence."""
    c,rx,lock=radio.modules();stack.enter_context(lock(timeout=3))
    if c.identity()!=request['identity']:raise ValueError('BOARD_IDENTITY_CHANGED')
    hw=session_hardware_context(stack,c,directory,request['identity'])
    state=c.read_json(c.STATE)
    if (state['identity']!=request['identity'] or state['configuration_id']!=request['configuration_id']
            or state['profile_id']!=request['profile_id'] or c.snapshot(hw)!=request['actual']):
        raise ValueError('SESSION_CONFIGURATION_CHANGED')
    rx.stopped_tx(hw)
    return c,rx,hw,state


def notify(message):
    """Send native READY/Watchdog to systemd; expired heartbeats cannot renew liveness."""
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:return
    if address.startswith('@'):address='\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as wire:
        wire.connect(address);wire.sendall(message.encode())


TUNE_SECONDS = 35
"""Original off-policy and uncalibrated tuning budget, in seconds."""
AUTO_TUNE_SECONDS = 180
"""Native auto-tuning budget including firmware calibration, in seconds; not a fixed sleep."""


@contextmanager
def supervised_tune(directory, policy):
    """Supervise heartbeat/deadline during blocking auto writes without refreshing hardware/data permission.

    Stop renewing watchdog on stop, expiry or exhausted budget. Blocking sysfs can delay Python signals,
    so the host must await independent post; firmware cancellation is not guaranteed to be immediate.
    """
    seconds = AUTO_TUNE_SECONDS if policy == 'auto' else TUNE_SECONDS
    stop = Event(); errors = []; started = time.monotonic()
    def supervise():
        """Read atomic host heartbeat only; stop renewing on expiry; never access SPI/sysfs."""
        try:
            while not stop.wait(.5):
                if time.monotonic() - started >= seconds or (directory/'stop').exists():return
                heartbeat = json.loads((directory/'heartbeat.json').read_text())
                heartbeat_age(heartbeat, time.monotonic_ns())
                notify('WATCHDOG=1')
        except Exception as error:
            errors.append(error)
    thread = Thread(target=supervise, name='h102-auto-tune-watchdog', daemon=True) if policy == 'auto' else None
    signal.alarm(seconds)
    if thread:thread.start()
    try:
        yield seconds
        if errors:raise errors[0]
    finally:
        stop.set()
        if thread:thread.join(timeout=1)
        signal.alarm(0)


def heartbeat_age(value,now_ns):
    """Use board monotonic time; reject future/expired heartbeats without cross-host wall clocks."""
    age=(now_ns-value['updated_ns'])/1e9
    if not 0<=age<TTL:raise TimeoutError('HOST_HEARTBEAT_EXPIRED')
    return age


def validate_sweep_batch(plan,centers,request,setup=False):
    """Independently verify actual rate/bandwidth and bounded batches on-board, beyond GUI validation."""
    channel=request['channels'][0]
    actual=next(v for v in request['actual']['parameters']['rx'] if v['channel']==channel)
    if (plan['sample_rate_hz']!=actual['sample_rate_hz'] or plan['bandwidth_hz']!=actual['rf_bandwidth_hz']
            or plan['fft_points'] not in (1024,2048,4096,8192,16384)
            or type(plan['averages']) is not int or not 1<=plan['averages']<=32
            or type(plan['settle_ms']) is not int or not 0<=plan['settle_ms']<=2000
            or not 2<=plan['grid_size']<=65536 or not 30_000_000<=plan['start_hz']<plan['stop_hz']<=6_000_000_000
            or not (len(centers)==0 if setup else 1<=len(centers)<=32)
            or any(type(x) is not int or not plan['start_hz']<=x<=plan['stop_hz'] for x in centers)):
        raise ValueError('SWEEP_BATCH_PLAN')


def apply_sweep_carrier(hw,current,channels,frequency,check):
    """Write/read each carrier synchronously; verify both channels and all non-LO settings without policy changes."""
    check();hw.write(hw.alt('adrv9002-phy',channels[0]-1,'frequency'),str(frequency),0)
    expected=radio.desired_settings(current,hw,channels,frequency)
    # With off policy, the driver restores shared-LO port states synchronously; verify all settings after the batch.
    # Auto calibration can change active gain: restore/check each segment; never disable calibration for speed.
    if current[str(hw.phy/'initial_calibrations')]=='auto':
        for channel in channels:
            gain=hw.phy/f'in_voltage{channel-1}_hardwaregain'
            mode=str(hw.phy/f'in_voltage{channel-1}_gain_control_mode')
            if str(gain) in current and current.get(mode)=='spi' and hw.read(gain)!=current[str(gain)]:
                check();hw.write(gain,current[str(gain)].split()[0],.001)
    return expected


def actor(directory,post=False):
    """Native main holds three locks until stop; independent post restores state and retains failures."""
    actor_started=time.monotonic()
    def interrupted(signum,frame):raise TimeoutError('NATIVE_SESSION_INTERRUPTED')
    signal.signal(signal.SIGALRM,interrupted);signal.signal(signal.SIGTERM,interrupted)
    with ExitStack() as stack:
        if post:
            c,rx,_,request=load(directory)
            c,rx,hw,state=locked(stack,directory,request)
        else:
            # Perform full checks once in the final locked actor; avoid duplicate hardware contexts in the control process.
            request,c,rx,hw,state=prepare(directory,stack)
            load(directory)
        if post:
            signal.alarm(35)
            original=request['settings']
            previous=c.read_json(directory/'current.json') if (directory/'current.json').exists() else original
            # Manual reception retains the last confirmed frequency; sweep restores the original frequency.
            before=original if request['mode']=='sweep' else previous
            target=c.read_json(directory/'target.json') if (directory/'target.json').exists() else None
            current=hw.settings()
            group=request['group'] if request['mode']=='sweep' else (target['channels'] if target else [])
            tx_channels=target.get('tx_channels',[]) if target else []
            failure=c.read_json(directory/'failed-state.json') if (directory/'failed-state.json').exists() else None
            radio.validate_restore_scene(hw,before,current,group,target['frequency_hz'] if target else None,previous,
                                         failed=failure.get('settings') if failure else None,tx_channels=tx_channels)
            idle_tune=request['mode']=='idle_tune'
            result=dict(passed=True,data_buffers_created=False) if idle_tune else rx.restore_rx(hw,request['rx_settings'])
            if not result['passed']:raise RuntimeError('RX_RESTORE_FAILED')
            if not idle_tune:result['network_kernel_buffers']=rx.restore_network_kernel_buffers()
            if request['pattern']=='ramp16':
                result['ssi_restore_errors']=rx.set_ssi_modes(hw,request['ssi_modes'])
                if result['ssi_restore_errors']:raise RuntimeError('SSI_RESTORE_FAILED')
            def check(calibrated_tx=False):
                hw.verify(complete=False)
                if calibrated_tx:hw.calibration_idle()
                else:hw.idle();rx.stopped_tx(hw)
            if group and (failure or hw.settings()!=before):radio.restore_carrier(hw,before,group,check,tx_channels)
            check();hw.verify(complete=True)
            result.update(configuration_unchanged=c.snapshot(hw)==request['actual'],
                          settings_restored=hw.settings()==before,frequency_retained=request['mode'] in ('live','idle_tune'),radio=radio.describe(hw,state,c.CATALOG))
            result['passed']=result['passed'] and result['configuration_unchanged'] and result['settings_restored']
            radio.save(directory/'post.json',result)
            if not result['passed']:raise RuntimeError('SESSION_POST_FAILED')
            signal.alarm(0);return
        hw.idle()
        if hw.settings()!=request['settings']:raise RuntimeError('INITIAL_SETTINGS_CHANGED')
        if request['pattern']=='ramp16':
            errors=rx.set_ssi_modes(hw,{p:'TESTMODE_DATA_RAMP_16_BIT' for p in request['ssi_modes']})
            if errors:raise RuntimeError('SSI_PATTERN_CONFIG_FAILED')
        ready=dict(pid=os.getpid(),configuration_id=request['configuration_id'],identity=request['identity'],
                   preparation_seconds=time.monotonic()-request['prepare_started_monotonic'],
                   actor_setup_seconds=request['prepare_started_monotonic']-actor_started,
                   controller_to_actor_seconds=actor_started-c.read_json(directory/'controller.json')['started_monotonic'],
                   mode=request['mode'],issued_ns=time.monotonic_ns(),heartbeat_ttl_seconds=TTL)
        if 'connection_snapshot' in request:
            ready.update(connection_snapshot=request['connection_snapshot'],channels=request['channels'])
        radio.save(directory/'ready.json',ready)
        current=request['settings'];last_command=0;next_check=0;guard=None;local=None;local_key=None
        notify('READY=1')
        try:
            while not (directory/'stop').exists():
                heartbeat=c.read_json(directory/'heartbeat.json');now=time.monotonic_ns()
                heartbeat_age(heartbeat,now)
                if time.monotonic()>=next_check:
                    hw.verify(complete=False);rx.stopped_tx(hw)
                    if guard is not None:guard.poll()
                    active=c.read_json(c.STATE)
                    if (active['configuration_id']!=request['configuration_id'] or active['identity']!=request['identity']
                            or c.snapshot(hw)!=request['actual']):raise RuntimeError('SESSION_CONFIGURATION_CHANGED')
                    for i in (0,1):
                        p=hw.alt('adrv9002-phy',i,'frequency')
                        if hw.read(p)!=current[str(p)]:raise RuntimeError('RX_CENTER_FREQUENCY_CHANGED')
                    rx.atomic_save(directory/'health.json',dict(checked_ns=now,permitted=True,ready=ready),exclusive=False)
                    notify('WATCHDOG=1');next_check=time.monotonic()+.5
                if (directory/'command.json').exists():
                    command=c.read_json(directory/'command.json')
                    if command['sequence']>last_command:
                        last_command=command['sequence']
                        if command.get('action') in ('guard_begin','guard_arm','guard_end'):
                            if request['mode']!='live':raise ValueError('GUARD_LIVE_ONLY')
                            channel=command['channel']
                            if channel not in request['channels']:raise ValueError('GUARD_CHANNEL_NOT_SELECTED')
                            hw.verify(complete=False);rx.stopped_tx(hw)
                            if guard is None:guard=stack.enter_context(capture.OverflowGuard(hw,request['identity']))
                            result=getattr(guard,command['action'][6:])(channel)
                            rx.atomic_save(directory/('guard-rx%d.json'%channel),result,exclusive=False)
                            rx.atomic_save(directory/'reply.json',dict(sequence=last_command,permitted=True,guard=result),exclusive=False)
                            next_check=0;continue
                        if command.get('action') in ('sweep_setup','sweep_batch'):
                            if request['mode']!='sweep':raise ValueError('SWEEP_BATCH_MODE')
                            plan=command['plan'];centers=command['centers']
                            validate_sweep_batch(plan,centers,request,command['action']=='sweep_setup')
                            key=(request['channels'][0],plan['fft_points'])
                            if local_key!=key:
                                if local is not None:local.close()
                                local=stack.enter_context(capture.LocalCapture(*key));local_key=key
                            def fast_check():
                                if (directory/'stop').exists():raise InterruptedError('SWEEP_STOP_REQUESTED')
                                heartbeat_age(c.read_json(directory/'heartbeat.json'),time.monotonic_ns())
                            segments=[];stopped=False;records=[]
                            for frequency in centers:
                                started=time.monotonic();records=[]
                                try:
                                    fast_check();hw.verify(complete=False);local.verify_sources(hw);rx.stopped_tx(hw)
                                    rx.atomic_save(directory/'target.json',dict(frequency_hz=frequency,channels=request['group']),exclusive=False)
                                    with supervised_tune(directory,current[str(hw.phy/'initial_calibrations')]):
                                        with hw.defer_write_audit(records,limit=5):
                                            current=apply_sweep_carrier(hw,current,request['group'],frequency,fast_check)
                                    tuned=time.monotonic()
                                    until=tuned+plan['settle_ms']/1000
                                    while time.monotonic()<until:
                                        fast_check();time.sleep(min(.002,max(0,until-time.monotonic())))
                                    power=local.power(plan['averages'],fast_check)
                                    item=capture.grid_cells(power,frequency,plan,local.offsets)
                                    item.update(center_hz=frequency,total_seconds=time.monotonic()-started,
                                        tune_seconds=tuned-started,capture_settle_fft_seconds=time.monotonic()-tuned,
                                        board_applied_ns=int(tuned*1e9),averaged_blocks=plan['averages'],discarded_blocks=3,
                                        samples_per_block=plan['fft_points'],capture_timings=dict(local.last_timings))
                                    segments.append(item)
                                except InterruptedError:
                                    stopped=True;break
                                finally:
                                    rx.atomic_save(directory/'current.json',current,exclusive=False)
                                notify('WATCHDOG=1')
                            rx.atomic_save(directory/'last-tune-writes.json',records,exclusive=False)
                            hw.verify(complete=False);local.verify_sources(hw);rx.stopped_tx(hw)
                            if hw.settings()!=current or c.snapshot(hw)!=request['actual']:raise RuntimeError('SWEEP_BATCH_SETTINGS_CHANGED')
                            rx.atomic_save(directory/'reply.json',dict(sequence=last_command,permitted=True,
                                segments=segments,stopped=stopped,radio=radio.describe(hw,state,c.CATALOG),physical_rf_verified=False),exclusive=False)
                            # The batch checked identity/TX/all settings/parameters; renew native health without redundant full checks.
                            rx.atomic_save(directory/'health.json',dict(checked_ns=time.monotonic_ns(),permitted=True,ready=ready),exclusive=False)
                            next_check=time.monotonic()+.5;continue
                        if command.get('action')=='activate_rx':
                            # Persist the request before converting idle control to data ownership so post can recover exceptions.
                            activation=command['activation'];selected=activation['channels'];pattern=activation['pattern']
                            actual=c.snapshot(hw);mapping=radio.describe(hw,state,c.CATALOG);info=activation['info']
                            if (request['mode']!='idle_tune' or selected not in ([1],[2],[1,2])
                                    or pattern not in ('normal','ramp16') or info['boot_id']!=request['identity']['boot_id']
                                    or info['configuration_id']!=request['configuration_id'] or info['active']!=request['profile_id']
                                    or info['parameters']!=actual['parameters'] or info['radio']!=mapping):
                                raise ValueError('RX_ACTIVATION_EXPECTED_CHANGED')
                            if sum(v['sample_rate_hz']*4 for v in actual['parameters']['rx'] if v['channel'] in selected)>64_000_000:
                                raise ValueError('RX_ACTIVATION_NETWORK_BUDGET')
                            hw.verify(complete=False);hw.idle();rx.stopped_tx(hw)
                            request.update(mode='live',pattern=pattern,channels=selected,group=[])
                            rx.atomic_save(directory/'request.json',request,exclusive=False)
                            rx.restore_network_kernel_buffers()
                            if pattern=='ramp16':
                                errors=rx.set_ssi_modes(hw,{p:'TESTMODE_DATA_RAMP_16_BIT' for p in request['ssi_modes']})
                                if errors:raise RuntimeError('SSI_ACTIVATION_FAILED: '+str(errors))
                            ready.update(mode='live',issued_ns=time.monotonic_ns())
                            rx.atomic_save(directory/'ready.json',ready,exclusive=False)
                            rx.atomic_save(directory/'reply.json',dict(sequence=last_command,radio=mapping,permitted=True,
                                activated_rx=True,ready=ready,physical_rf_verified=False),exclusive=False)
                            next_check=0;continue
                        group=command.get('channels') or request['group']
                        choices=radio.describe(hw,state,c.CATALOG)['groups']
                        if not any(g['editable'] and g['channels']==group for g in choices):raise ValueError('TUNE_GROUP_INVALID')
                        tx_channels=next(g['tx_shared'] for g in choices if g['channels']==group)
                        if request['mode']=='sweep' and tx_channels:raise ValueError('SWEEP_REQUIRES_INDEPENDENT_LO')
                        if request['mode']=='sweep' and group!=request['group']:raise ValueError('SWEEP_GROUP_CHANGED')
                        frequency=command['frequency_hz']
                        def check(calibrated_tx=False):
                            heartbeat_age(c.read_json(directory/'heartbeat.json'),time.monotonic_ns())
                            hw.verify(complete=False)
                            if calibrated_tx:hw.calibration_idle()
                            else:hw.idle();rx.stopped_tx(hw)
                        check();rx.atomic_save(directory/'target.json',dict(frequency_hz=frequency,channels=group,tx_channels=tx_channels),exclusive=False)
                        # This transaction has bounded compact auditing; writes remain immediate without claiming full historical logs.
                        records=[];tune_timings={}
                        policy=current[str(hw.phy/'initial_calibrations')]
                        with supervised_tune(directory,policy) as seconds:
                            rx.atomic_save(directory/'tune-budget.json',dict(policy=policy,seconds=seconds,
                                data_permission_refreshed=False,issued_monotonic=time.monotonic()),exclusive=False)
                            # Shared-LO carrier/calibration/state changes retain the default per-write
                            # writes.jsonl audit; do not increase RAM log limits or skip write records.
                            with (nullcontext() if tx_channels else hw.defer_write_audit(records,limit=5)):
                                current=radio.apply_carrier(hw,current,group,frequency,check,tune_timings,tx_channels)
                        rx.atomic_save(directory/'last-tune-writes.json',records,exclusive=False)
                        rx.atomic_save(directory/'last-tune-audit.json',dict(storage='writes.jsonl' if tx_channels else 'last-tune-writes.json',
                            complete_history=False),exclusive=False)
                        rx.atomic_save(directory/'current.json',current,exclusive=False)
                        check()
                        if c.snapshot(hw)!=request['actual']:raise RuntimeError('PARAMETERS_AFTER_TUNE_CHANGED')
                        rx.atomic_save(directory/'reply.json',dict(sequence=last_command,radio=radio.describe(hw,state,c.CATALOG),
                            applied_ns=time.monotonic_ns(),permitted=True,physical_rf_verified=False,timings=tune_timings),exclusive=False)
                        signal.alarm(0);next_check=0
                # Poll sweep commands at 1 ms; retain the 500 ms hardware check and calibration deadlines.
                time.sleep(.001 if request['mode']=='sweep' else .1)
            if local is not None:local.close()
            radio.save(directory/'main.json',dict(status='stopped',last_command=last_command))
        except Exception as error:
            try:radio.save(directory/'failed-state.json',dict(settings=hw.settings(),error=str(error),last_command=last_command))
            except Exception as capture_error:radio.save(directory/'failed-state-error.json',dict(error=str(capture_error)))
            radio.save(directory/'main.json',dict(status='failed',error=str(error),last_command=last_command));raise


def prepare(directory,stack):
    """The final actor holds all three locks, validates fully and records original settings before granting permission."""
    c,rx,lock=radio.modules();c.private(directory,directory=True)
    c.private(directory/'controller.json')
    c.private(directory/'expected.json');expected=c.read_json(directory/'expected.json')
    if expected['mode'] not in ('live','sweep','idle_tune') or expected['pattern'] not in ('normal','ramp16'):
        raise ValueError('SESSION_MODE_OR_SOURCE')
    if expected['mode'] in ('sweep','idle_tune') and expected['pattern']!='normal':raise ValueError('TUNE_NORMAL_ONLY')
    started=time.monotonic()
    stack.enter_context(lock(timeout=3));identity=c.identity()
    hw=session_hardware_context(stack,c,directory,identity);hw.idle();rx.stopped_tx(hw)
    state=c.read_json(c.STATE);actual=c.snapshot(hw)
    info=expected['info'];package=c.load_package(c.CATALOG,state['profile_id'],trusted=True)
    c.verify_snapshot(actual,package)
    if (state['identity']!=identity or state['actual']!=actual or state['sha256']!=package['manifest']['sha256']
            or info['boot_id']!=identity['boot_id'] or info['configuration_id']!=state['configuration_id']
            or info['active']!=state['profile_id'] or info['parameters']!=actual['parameters']):
        raise ValueError('EXPECTED_CONFIGURATION_CHANGED')
    mapping=radio.describe(hw,state,c.CATALOG)
    discovery=expected.get('discover_radio',False)
    if type(discovery) is not bool or (discovery and expected['mode']!='idle_tune'):
        raise ValueError('RADIO_DISCOVERY_MODE')
    if not discovery and mapping!=info['radio']:raise ValueError('EXPECTED_RADIO_CHANGED')
    channels=expected['channels']
    if channels not in ([1],[2],[1,2]):raise ValueError('RX_CHANNELS')
    group=[]
    if expected['mode']=='idle_tune':
        choice=(next((g for g in mapping['groups'] if g['editable']),None) if discovery else
                next((g for g in mapping['groups'] if g['channels']==channels),None))
        if discovery:
            channels=choice['channels'] if choice else channels
        elif not choice or not choice['editable']:raise ValueError('IDLE_TUNE_GROUP')
        group=channels if choice else []
        if group and len({mapping['rx_center_frequency_hz'][str(i)] for i in group})!=1:raise ValueError('SHARED_LO_INCONSISTENT')
    if expected['mode']=='sweep':
        if len(channels)!=1:raise ValueError('SWEEP_SINGLE_RX_ONLY')
        choice=next(g for g in mapping['groups'] if channels[0] in g['channels'])
        if not choice['editable'] or choice['tx_shared']:raise ValueError('SWEEP_TX_SHARED_LO')
        group=choice['channels']
        if len({mapping['rx_center_frequency_hz'][str(i)] for i in group})!=1:raise ValueError('SHARED_LO_INCONSISTENT')
    modes=rx.ssi_modes(hw)
    if set(modes.values())!={'TESTMODE_DATA_NORMAL'}:raise ValueError('FOREIGN_SSI_MODE')
    request=dict(identity=identity,actual=actual,configuration_id=state['configuration_id'],profile_id=state['profile_id'],
        settings=hw.settings(),rx_settings=rx.rx_settings(hw),ssi_modes=modes,mode=expected['mode'],
        pattern=expected['pattern'],channels=channels,group=group,
        unit=c.read_json(directory/'controller.json')['unit'],
        controller_pid=c.read_json(directory/'controller.json')['pid'],prepare_started_monotonic=started,
        sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (directory/'board_session.py',directory/'board_radio.py',directory/'board_capture.py')})
    if discovery:
        image_path=Path('/etc/h102/sdk1-image.json')
        image=c.read_json(image_path)
        # Large image module manifests stay out of bounded control snapshots; include their identity and full SHA.
        summary={k:v for k,v in image.items() if isinstance(v,(str,int,float,bool)) or v is None}
        request['connection_snapshot']=dict(identity=identity,parameters=actual['parameters'],radio=mapping,
            data_idle=True,kernel=os.uname().release,image=summary,image_metadata_scope='scalar_summary',
            image_manifest_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest())
    radio.save(directory/'request.json',request)
    if expected['mode']!='idle_tune':rx.restore_network_kernel_buffers()
    return request,c,rx,hw,state


def emit(value):
    """Emit one bounded JSON response; do not accumulate raw data or unlimited health logs over SSH."""
    print(json.dumps(value),flush=True)


class ControlFiles:
    """Bridge root-private JSON only; the locked actor retains hardware dependencies and full checks."""
    @staticmethod
    def private(path, directory=False):
        """Check every ancestor's root ownership, type and write permissions like the released profile entry point."""
        path=Path(path).absolute()
        for item in (path,)+tuple(path.parents):
            info=item.lstat()
            kind=stat.S_ISDIR if item!=path or directory else stat.S_ISREG
            if info.st_uid!=0 or info.st_mode&0o022 or not kind(info.st_mode):
                raise ValueError('UNTRUSTED_PATH: '+str(item))

    @staticmethod
    def read_json(path):
        """Require object JSON; existing protocol validation checks control fields."""
        value=json.loads(Path(path).read_bytes())
        if not isinstance(value,dict):raise ValueError('JSON_OBJECT_REQUIRED')
        return value


def control_file_tools():
    """Load hardware-free atomic storage tools only; avoid importing all board dependencies in the bridge."""
    sys.path.insert(0,'/usr/local/lib/h102/sdk1/scripts')
    import h102_rf_usb_tx_storage as storage
    return ControlFiles,storage


def control(directory):
    """Receive host heartbeat/tune/stop on SSH stdin; EOF revokes permission without self-renewal."""
    started=time.monotonic()
    c,rx=control_file_tools();c.private(directory,directory=True)
    emit(dict(type='bootstrap'))
    request=dict(unit='h102-gui-session-'+uuid.uuid4().hex+'.service',pid=os.getpid(),started_monotonic=started)
    radio.save(directory/'controller.json',request)
    rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=True)
    base=['/usr/bin/python3','-B',str(Path(__file__).absolute())]
    process=subprocess.Popen(['systemd-run','--unit='+request['unit'],'--property=Type=notify',
        '--property=WatchdogSec=60','--property=TimeoutStartSec=30','--property=TimeoutStopSec=45',
        '--property=Restart=no','--property=UMask=0077','--property=ExecStopPost='+shlex.join(base+['post','--directory',str(directory)]),
        '--wait']+base+['run','--directory',str(directory)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    sent_ready=False;sequence=0;pending=None;eof=False;buffer=b''
    sweep_mode=c.read_json(directory/'expected.json')['mode']=='sweep'
    try:
        while process.poll() is None:
            if not sent_ready and (directory/'ready.json').exists() and (directory/'health.json').exists():
                emit(dict(type='ready',ready=c.read_json(directory/'ready.json'),
                          health=c.read_json(directory/'health.json'),now_ns=time.monotonic_ns()));sent_ready=True
            if pending and (directory/'reply.json').exists():
                reply=c.read_json(directory/'reply.json')
                if reply['sequence']==sequence:
                    emit(dict(type='reply',id=pending,result=reply));pending=None
            if not eof and select.select([sys.stdin.buffer],[],[],(.002 if pending else .01) if sweep_mode else (.01 if pending else .1))[0]:
                chunk=os.read(sys.stdin.fileno(),8192)
                if not chunk:
                    eof=True;(directory/'stop').touch(mode=0o600,exist_ok=True)
                else:
                    buffer+=chunk
                    if len(buffer)>16384:raise ValueError('CONTROL_LINE_TOO_LARGE')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1);value=json.loads(line)
                        action=value['action']
                        if action=='pulse':
                            rx.atomic_save(directory/'heartbeat.json',dict(updated_ns=time.monotonic_ns()),exclusive=False)
                            health=c.read_json(directory/'health.json') if (directory/'health.json').exists() else None
                            emit(dict(type='pulse',health=health,now_ns=time.monotonic_ns()))
                        elif action=='stop':(directory/'stop').touch(mode=0o600,exist_ok=True)
                        elif action=='tune':
                            if pending:raise RuntimeError('TUNE_ALREADY_PENDING')
                            hz=value['frequency_hz']
                            if type(hz) is not int or not radio.MIN_HZ<=hz<=radio.MAX_HZ:raise ValueError('FREQUENCY_RANGE')
                            sequence+=1;pending=value['id']
                            rx.atomic_save(directory/'command.json',dict(sequence=sequence,frequency_hz=hz,channels=value.get('channels')),exclusive=False)
                        elif action=='activate_rx':
                            if pending:raise RuntimeError('CONTROL_ALREADY_PENDING')
                            sequence+=1;pending=value['id']
                            rx.atomic_save(directory/'command.json',dict(sequence=sequence,action=action,
                                activation={k:value[k] for k in ('info','channels','pattern')}),exclusive=False)
                        elif action in ('guard_begin','guard_arm','guard_end','sweep_setup','sweep_batch'):
                            if pending:raise RuntimeError('CONTROL_ALREADY_PENDING')
                            sequence+=1;pending=value['id']
                            payload={k:v for k,v in value.items() if k!='id'}
                            payload['sequence']=sequence
                            rx.atomic_save(directory/'command.json',payload,exclusive=False)
                        else:raise ValueError('CONTROL_ACTION')
            elif eof:time.sleep(.1)
        out,err=process.communicate(timeout=2)
        records={n:c.read_json(directory/n) for n in ('request.json','ready.json','main.json','post.json','failed-state.json','tune-budget.json') if (directory/n).exists()}
        result=dict(returncode=process.returncode,stdout=out.decode(errors='replace'),stderr=err.decode(errors='replace'),
                    unit=request['unit'],records=records)
        radio.save(directory/'manager.json',result);emit(dict(type='finished',result=result))
    finally:
        # Manager exit stops renewals; independent actor TTL/watchdog/post remain effective.
        if process.poll() is None:(directory/'stop').touch(mode=0o600,exist_ok=True)


def warm(directory):
    """Load private modules and wait for start without board locks, permissions or hardware access.

    Lines are bounded to 16 KiB. Exit after 15 s without input or on EOF. pulse keeps the control channel only;
    start establishes expected identity and the native owner's independent heartbeat/recovery duties.
    """
    c,_=control_file_tools();c.private(directory,directory=True)
    for name in ('board_session.py','board_radio.py','board_capture.py'):c.private(directory/name)
    emit(dict(type='warm'))
    buffer=b'';last=time.monotonic()
    while time.monotonic()-last<15:
        if not select.select([sys.stdin.buffer],[],[],.1)[0]:continue
        chunk=os.read(sys.stdin.fileno(),8192)
        if not chunk:return
        buffer+=chunk
        if len(buffer)>16384:raise ValueError('WARM_LINE_TOO_LARGE')
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
    """Root-private entry point; released SDK1 functions define the hardware boundary."""
    p=argparse.ArgumentParser();p.add_argument('action',choices=['warm','control','run','post'])
    p.add_argument('--directory',type=Path,required=True);args=p.parse_args()
    if os.geteuid()!=0:raise PermissionError('ROOT_REQUIRED')
    if args.directory.parent!=Path('/run') or not args.directory.name.startswith('h102-gui-session-'):
        raise ValueError('PRIVATE_RUN_DIRECTORY_REQUIRED')
    os.umask(0o077)
    if args.action=='warm':warm(args.directory)
    elif args.action=='control':control(args.directory)
    else:actor(args.directory,args.action=='post')


if __name__=='__main__':main()

# End of file: board_session.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
