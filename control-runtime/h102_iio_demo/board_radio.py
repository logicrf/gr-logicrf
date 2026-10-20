#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       board_radio.py
@brief      Inspect LO groups and perform stopped-RX tuning transactions through existing SDK1 interfaces.
@details    Support independent RX LO or explicitly shared four-channel LO2; tune RX/TX carriers together with sources off.
            Independent systemd main/post transactions perform tuning and rollback on failure.
            Each stage holds configuration/RX/GNSS locks and binds boot, profile and original settings; never enable TX or DDS.
            Driver carrier readback does not establish physical RF accuracy. Run temporarily in a root-private /run directory.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.5.0
@date       2026-10-15
"""
from contextlib import ExitStack, contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid


MIN_HZ, MAX_HZ = 30_000_000, 6_000_000_000
"""Chip-internal LO limits in Hz; input limits do not qualify whole-board RF performance across the band."""


def modules():
    """Load released interfaces on the board; offline host import does not touch hardware."""
    sys.path.insert(0, '/usr/local/lib/h102/sdk1/scripts')
    import h102_profile_config as config
    import h102_iio_rx_lease as rx
    from h102_iio_owner_lock import configuration_lock
    config.configure_stack('sdk1-candidate')
    return config, rx, configuration_lock


def save(path, value):
    """Create root receipts for this transaction only; never overwrite an existing receipt."""
    with path.open('x') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write('\n');out.flush();os.fsync(out.fileno())
    path.chmod(0o600)


def describe(hw, state, catalog):
    """Read package LO groups and driver carriers; equal readbacks alone do not imply LO sharing."""
    profile=json.loads((catalog/state['profile_id']/'profile.json').read_text())
    clocks=profile['clocks']
    mapping={f'{port}{i}LoSelect':int(clocks[f'{port}{i}LoSelect']) for port in ('rx','tx') for i in (1,2)}
    if any(v not in (1,2) for v in mapping.values()):
        raise ValueError('Only internal LO1/LO2 profiles are supported')
    frequencies={str(i+1):int(hw.read(hw.alt('adrv9002-phy',i,'frequency'))) for i in (0,1)}
    groups=[]
    for lo in sorted({mapping['rx1LoSelect'],mapping['rx2LoSelect']}):
        channels=[i for i in (1,2) if mapping[f'rx{i}LoSelect']==lo]
        tx=[i for i in (1,2) if mapping[f'tx{i}LoSelect']==lo]
        groups.append(dict(lo=lo,channels=channels,tx_shared=tx,
                           editable=not tx or (set(mapping.values())=={2} and hasattr(hw,'calibration_idle'))))
    return dict(mapping=mapping,rx_center_frequency_hz=frequencies,groups=groups,
                tx_center_frequency_hz={str(i+1):int(hw.read(hw.alt('adrv9002-phy',i+2,'frequency'))) for i in (0,1)},
                initial_calibrations=hw.read(hw.phy/'initial_calibrations'),
                verification='driver Carrier readback; physical RF frequency NOT_VERIFIED')


def desired_settings(before, hw, channels, frequency, tx_channels=()):
    """Change this RX group and explicitly shared TX carriers only; restore calibration policy and other settings."""
    expected=dict(before)
    for channel in channels:
        expected[str(hw.alt('adrv9002-phy',channel-1,'frequency'))]=str(frequency)
    for channel in tx_channels:
        expected[str(hw.alt('adrv9002-phy',channel+1,'frequency'))]=str(frequency)
    return expected


def run_initial_calibrations(hw, policy, check, timings=None):
    """Execute one command-style run and verify policy retention; run is not a persistent readable attribute."""
    check();original=hw.read(policy);started=time.monotonic()
    record=dict(monotonic=started,path=str(policy),requested='run',expected=original,
                operation='initial_calibration_command')
    try:
        policy.write_text('run\n')
        actual=hw.read(policy);record.update(actual=actual,readback_ok=actual==original)
    except Exception as error:
        hw.record_write(dict(record,write_error=repr(error),readback_ok=False));raise
    hw.record_write(record)
    if not record['readback_ok']:raise RuntimeError('INITIAL_CALIBRATION_POLICY_CHANGED')
    check()
    if timings is not None:timings['joint_initial_calibration_seconds']=time.monotonic()-started


@contextmanager
def shared_tune_power(hw, channels, tx_channels, check):
    """Keep TX powered in CALIBRATED only for this tune; always power down on exit without granting RF/data permission."""
    if not tx_channels:
        yield check
        return
    check()
    try:
        for channel in channels:
            check();hw.write(hw.phy/f'in_voltage{channel-1}_ensm_mode','calibrated')
        for channel in tx_channels:
            check();hw.write(hw.phy/f'out_voltage{channel-1}_ensm_mode','calibrated')
            # State_Get reports CAL after PowerDown, while the driver's cached_state may still contain the old RF state.
            # Rewriting the original attenuation-control mode caches actual CAL and prevents PowerUp returning to RF_ENABLED.
            path=hw.phy/f'out_voltage{channel-1}_atten_control_mode'
            check();hw.write(path,hw.read(path))
        def quiet_check():check(True)
        for channel in tx_channels:
            quiet_check();hw.write(hw.phy/f'out_voltage{channel-1}_en','1')
        quiet_check()
        yield quiet_check
    finally:
        errors=hw.stop()
        if errors:raise RuntimeError('SHARED_TUNE_SOURCE_OFF_FAILED: '+str(errors))
        check()


def apply_carrier(hw, before, channels, frequency, check, timings=None, tx_channels=()):
    """Tune once using the actual driver's shared-LO handler, follow the original calibration policy, then check all settings.

    check verifies identity/deadline before every write; True permits bounded CALIBRATED power only, never RF.
    Independent post restores before on failure; do not retry implicitly or change the target here.
    """
    if type(frequency) is not int or not MIN_HZ<=frequency<=MAX_HZ or not channels:
        raise ValueError('Invalid RX center frequency or group')
    started=time.monotonic()
    policy=hw.phy/'initial_calibrations'
    capability=hw.phy/'fdd_shared_lo_atomic'
    if tx_channels and capability.is_file() and hw.read(capability)=='1':
        # The new driver combines shared carrier changes, calibration and temporary power restoration in one sysfs call.
        # The GUI neither changes calibration policy nor keeps TX powered; strict source-off conditions still apply before/after.
        check();hw.write(policy,before[str(policy)])
        check();hw.write(hw.alt('adrv9002-phy',channels[0]-1,'frequency'),str(frequency),0)
        if timings is not None:timings['write_carrier_seconds']=time.monotonic()-started
        settings_started=time.monotonic()
        for channel in channels:
            gain=hw.phy/f'in_voltage{channel-1}_hardwaregain'
            mode=str(hw.phy/f'in_voltage{channel-1}_gain_control_mode')
            if str(gain) in before and before.get(mode)=='spi' and hw.read(gain)!=before[str(gain)]:
                check();hw.write(gain,before[str(gain)].split()[0],.001)
        check()
        expected=desired_settings(before,hw,channels,frequency,tx_channels)
        if hw.settings()!=expected:raise RuntimeError('Driver shared-LO readback or non-LO settings changed')
        if timings is not None:timings['verify_settings_seconds']=time.monotonic()-settings_started
        return expected
    # Updating both carrier ports takes two driver transactions. Defer a large auto-calibration until the carriers agree,
    # then issue one full run; calibrating with new RX/old TX on a shared LO can invalidate Prime.
    deferred=bool(tx_channels and before[str(policy)]=='auto' and
                  any(abs(int(before[str(hw.alt('adrv9002-phy',c-1,'frequency'))])-frequency)>=100_000_000 for c in channels))
    with shared_tune_power(hw,channels,tx_channels,check) as quiet:
        quiet();hw.write(policy,'off' if deferred else before[str(policy)])
        quiet();hw.write(hw.alt('adrv9002-phy',channels[0]-1,'frequency'),str(frequency),0)
        if tx_channels:
            quiet();hw.write(hw.alt('adrv9002-phy',tx_channels[0]+1,'frequency'),str(frequency),0)
        if deferred:
            quiet();hw.write(policy,before[str(policy)])
            run_initial_calibrations(hw,policy,quiet,timings)
        if timings is not None:timings['write_carrier_seconds']=time.monotonic()-started
        settings_started=time.monotonic()
        if tx_channels:
            for channel in channels:
                path=hw.phy/f'in_voltage{channel-1}_ensm_mode'
                quiet();hw.write(path,before[str(path)])
        for channel in channels:
            gain=hw.phy/f'in_voltage{channel-1}_hardwaregain'
            mode=str(hw.phy/f'in_voltage{channel-1}_gain_control_mode')
            if str(gain) in before and before.get(mode)=='spi' and hw.read(gain)!=before[str(gain)]:
                quiet();hw.write(gain,before[str(gain)].split()[0],.001)
        quiet();hw.write(policy,before[str(policy)])
    expected=desired_settings(before,hw,channels,frequency,tx_channels)
    if hw.settings()!=expected:
        raise RuntimeError('Tuning readback or non-LO settings changed')
    if timings is not None:timings['verify_settings_seconds']=time.monotonic()-settings_started
    return expected


def restore_carrier(hw, before, channels, check, tx_channels=()):
    """On failure restore original RX/shared TX carriers and RX state/gain/policy with sources off."""
    old=[int(before[str(hw.alt('adrv9002-phy',i-1,'frequency'))]) for i in channels]
    if len(set(old))!=1:
        raise RuntimeError('Original shared LO readbacks disagree; refusing to guess a rollback target')
    if any(int(before[str(hw.alt('adrv9002-phy',i+1,'frequency'))])!=old[0] for i in tx_channels):
        raise RuntimeError('Original shared RX/TX readbacks disagree; refusing to guess a rollback target')
    policy=hw.phy/'initial_calibrations'
    # Restore the known original frequency without repeating the failed calibration, then restore the user's policy.
    with shared_tune_power(hw,channels,tx_channels,check) as quiet:
        quiet();hw.write(policy,'off')
        quiet();hw.write(hw.alt('adrv9002-phy',channels[0]-1,'frequency'),str(old[0]),0)
        if tx_channels:
            quiet();hw.write(hw.alt('adrv9002-phy',tx_channels[0]+1,'frequency'),str(old[0]),0)
        for i in channels:
            p=hw.phy/f'in_voltage{i-1}_ensm_mode'
            quiet();hw.write(p,before[str(p)])
            gain=hw.phy/f'in_voltage{i-1}_hardwaregain'
            if str(gain) in before and hw.read(gain)!=before[str(gain)]:
                quiet();hw.write(gain,before[str(gain)].split()[0],.001)
        quiet();hw.write(policy,before[str(policy)])
    if hw.settings()!=before:
        raise RuntimeError('Incomplete RX tuning rollback readback')


def validate_restore_scene(hw,before,current,channels,target=None,previous=None,failed=None,tx_channels=()):
    """Accept only transaction-scoped RX differences; the failure snapshot must match current state exactly.

    The actor holding all three locks saves failed at the exception; arbitrary user-provided device state is not accepted.
    This function verifies without hardware writes; restore_carrier handles calibration-failure LO/gain transients.
    """
    if set(current)!=set(before):raise RuntimeError('FOREIGN_PARAMETERS_BEFORE_RESTORE')
    if failed is not None:
        if current!=failed:raise RuntimeError('FAILED_SCENE_CHANGED_BEFORE_RESTORE')
        owned={str(hw.phy/'initial_calibrations')}
        owned.update(str(hw.alt('adrv9002-phy',i+1,'frequency')) for i in tx_channels)
        for channel in channels:
            owned.add(str(hw.alt('adrv9002-phy',channel-1,'frequency')))
            owned.update(str(hw.phy/f'in_voltage{channel-1}_{key}') for key in ('ensm_mode','hardwaregain'))
        if any(v!=before[k] and k not in owned for k,v in current.items()):raise RuntimeError('FOREIGN_PARAMETERS_BEFORE_RESTORE')
        return
    previous=previous or before
    allowed={str(hw.phy/'initial_calibrations'):{before[str(hw.phy/'initial_calibrations')],'auto','off'}}
    for channel in tx_channels:
        lo=str(hw.alt('adrv9002-phy',channel+1,'frequency'))
        allowed[lo]={before[lo],previous[lo]}
        if target is not None:allowed[lo].add(str(target))
    for channel in channels:
        lo=str(hw.alt('adrv9002-phy',channel-1,'frequency'))
        allowed[lo]={before[lo],previous[lo]}
        if target is not None:allowed[lo].add(str(target))
        mode=str(hw.phy/f'in_voltage{channel-1}_ensm_mode')
        allowed[mode]={before[mode],'calibrated'}
    if any(v not in allowed.get(k,{before[k]}) for k,v in current.items()):raise RuntimeError('FOREIGN_PARAMETERS_BEFORE_RESTORE')


def locked(stack, directory, request):
    """Check identical identity/profile and all three locks for each main/post; reject writes while busy."""
    config,rx,lock=modules()
    stack.enter_context(lock(timeout=3))
    identity=config.identity()
    if identity!=request['identity']:
        raise ValueError('BOARD_IDENTITY_CHANGED')
    hw=config.hardware_context(stack,directory,identity)
    state=config.read_json(config.STATE)
    if (state['identity']!=identity or state['configuration_id']!=request['configuration_id']
            or state['profile_id']!=request['profile_id'] or config.snapshot(hw)!=request['parameters']):
        raise ValueError('Profile changed across the tuning transaction')
    hw.idle();rx.stopped_tx(hw)
    return config,rx,hw,state


def action(directory, post=False):
    """Independent native main/post: retain the new LO on success; restore original LO after failure/interruption."""
    config,_,_=modules()
    config.private(directory,directory=True);config.private(directory/'request.json')
    request=config.read_json(directory/'request.json')
    config.private(Path(__file__))
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=request['source_sha256']:
        raise ValueError('TUNER_SOURCE_CHANGED')
    deadline=time.monotonic()+35
    def interrupted(signum, frame):
        raise TimeoutError('RX_TUNE_INTERRUPTED')
    signal.signal(signal.SIGTERM,interrupted)
    with ExitStack() as stack:
        config,rx,hw,state=locked(stack,directory,request)
        def check(calibrated_tx=False):
            if time.monotonic()>=deadline:raise TimeoutError('RX_TUNE_DEADLINE')
            hw.verify(complete=False)
            if calibrated_tx:hw.calibration_idle()
            else:hw.idle();rx.stopped_tx(hw)
        if post:
            if not (directory/'before.json').exists():
                save(directory/'post.json',dict(passed=True,tuning_attempted=False));return
            before=config.read_json(directory/'before.json')['settings']
            if (directory/'committed.json').exists():
                committed=config.read_json(directory/'committed.json')
                if hw.settings()!=committed['settings']:raise RuntimeError('Settings drift after tuning')
                check();save(directory/'post.json',dict(passed=True,committed=True,radio=describe(hw,state,config.CATALOG)))
            else:
                # Roll back only this transaction's permitted differences; preserve external LO/gain changes.
                current=hw.settings()
                failure=config.read_json(directory/'failed-state.json') if (directory/'failed-state.json').exists() else None
                validate_restore_scene(hw,before,current,request['channels'],request['frequency_hz'],
                                       failed=failure['settings'] if failure else None,
                                       tx_channels=next(g['tx_shared'] for g in request['radio_before']['groups'] if g['channels']==request['channels']))
                restore_carrier(hw,before,request['channels'],check,
                                next(g['tx_shared'] for g in request['radio_before']['groups'] if g['channels']==request['channels']))
                check();save(directory/'post.json',dict(passed=True,committed=False,rolled_back=True,radio=describe(hw,state,config.CATALOG)))
            return
        radio=describe(hw,state,config.CATALOG)
        if radio!=request['radio_before']:
            raise ValueError('RX LO changed before commit; refresh device state')
        group=next(g for g in radio['groups'] if g['channels']==request['channels'])
        if not group['editable']:raise ValueError('RX shares its LO with TX; RX-only tuning is refused')
        if len({radio['rx_center_frequency_hz'][str(c)] for c in group['channels']})!=1:
            raise ValueError('Shared RX LO readbacks disagree')
        before=hw.settings()
        save(directory/'before.json',dict(settings=before,radio=radio,issued_monotonic=time.monotonic(),deadline_monotonic=deadline))
        try:
            expected=apply_carrier(hw,before,group['channels'],request['frequency_hz'],check,tx_channels=group['tx_shared'])
        except Exception as error:
            save(directory/'failed-state.json',dict(settings=hw.settings(),error=str(error)))
            raise
        check()
        save(directory/'committed.json',dict(settings=expected,radio=describe(hw,state,config.CATALOG),physical_rf_verified=False))


def manager(directory, expected, channels, frequency):
    """Check one root transaction, launch independent main/post, and return exact native exit/rollback receipts."""
    if os.geteuid()!=0:raise PermissionError('ROOT_REQUIRED')
    if directory.parent!=Path('/run') or not directory.name.startswith('h102-gui-rx-tune-'):
        raise ValueError('PRIVATE_RUN_DIRECTORY_REQUIRED')
    if type(frequency) is not int or not MIN_HZ<=frequency<=MAX_HZ:
        raise ValueError('RX center frequency must be integer Hz in 30-6000 MHz')
    config,rx,lock=modules();config.private(directory,directory=True)
    with ExitStack() as stack:
        stack.enter_context(lock(timeout=3));identity=config.identity()
        if identity['boot_id']!=expected['boot_id']:raise ValueError('BOOT_CHANGED')
        hw=config.hardware_context(stack,directory,identity);hw.idle();rx.stopped_tx(hw)
        state=config.read_json(config.STATE)
        actual=config.snapshot(hw)
        if (state['configuration_id']!=expected['configuration_id'] or state['profile_id']!=expected['active']
                or state['identity']!=identity or actual['parameters']!=expected['parameters']):
            raise ValueError('EXPECTED_CONFIGURATION_CHANGED')
        package=config.load_package(config.CATALOG,state['profile_id'],trusted=True)
        config.verify_snapshot(actual,package)
        if state['actual']!=actual or state['sha256']!=package['manifest']['sha256']:
            raise ValueError('ACTIVE_PACKAGE_MISMATCH')
        radio=describe(hw,state,config.CATALOG)
        if radio!=expected['radio']:raise ValueError('LO changed; refresh device state')
        group=next((g for g in radio['groups'] if g['channels']==channels),None)
        if not group or not group['editable']:raise ValueError('Selected RX LO cannot tune independently of TX')
        request=dict(identity=identity,configuration_id=state['configuration_id'],profile_id=state['profile_id'],
                     parameters=actual,radio_before=radio,channels=channels,frequency_hz=frequency,
                     source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        save(directory/'request.json',request)
    script=Path(__file__).absolute();unit='h102-gui-rx-tune-'+uuid.uuid4().hex+'.service'
    base=['/usr/bin/python3','-B',str(script)]
    post=shlex.join(base+['post','--directory',str(directory)])
    process=subprocess.run(['systemd-run','--unit='+unit,'--property=Type=exec','--property=Restart=no',
        '--property=RuntimeMaxSec=50','--property=TimeoutStopSec=45','--property=UMask=0077',
        '--property=ExecStopPost='+post,'--wait']+base+['run','--directory',str(directory)],
        capture_output=True,text=True,timeout=200)
    records={n:config.read_json(directory/n) for n in ['before.json','committed.json','post.json'] if (directory/n).exists()}
    result=dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr,unit=unit,records=records,
                passed=process.returncode==0 and records.get('post.json',{}).get('passed',False)
                       and bool(records.get('committed.json')),physical_rf_verified=False)
    save(directory/'manager.json',result);return result


def main():
    """Native main/post CLI for root-private directories; the host manager supplies verified requests."""
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','post']);p.add_argument('--directory',type=Path,required=True)
    args=p.parse_args()
    if os.geteuid()!=0:raise PermissionError('ROOT_REQUIRED')
    action(args.directory,args.action=='post')


if __name__=='__main__':main()

# End of file: board_radio.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
