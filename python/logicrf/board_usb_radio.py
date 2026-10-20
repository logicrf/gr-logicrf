"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file board_usb_radio.py
@brief Prepare actual RF plans inside USB manager locks and restore through independent outer-post.
@details Reuse fixed GUI tuning/recovery functions only, without cyclic IQ TX. The manager saves originals,
         tunes silently and freezes templates; the actor grants RF, disabling it before expensive post checks on error.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import hashlib
import json
import math
from pathlib import Path
import time
import board_tx as common
from board_tx import radio
from gr_usb_radio_plan import PLAN


def validate(plan):
    """Validate explicit RF switches and units/ranges; actual profile groups separately verify shared-LO agreement."""
    if type(plan['rf_enabled']) is not bool:raise ValueError('USB_RF_BOOLEAN_REQUIRED')
    if plan['rx_channels'] not in ([],[1],[2],[1,2]) or plan['tx_channels'] not in ([],[1],[2],[1,2]):
        raise ValueError('USB_RADIO_CHANNELS')
    if not (plan['rx_channels'] or plan['tx_channels']) or plan['rf_enabled'] and not plan['tx_channels']:
        raise ValueError('USB_RADIO_DIRECTIONS')
    for key in ('rx_frequency','tx_frequency'):
        if type(plan[key]) is not int or not 50_000_000<=plan[key]<=5_800_000_000:raise ValueError('USB_RADIO_FREQUENCY')
    attenuation=plan['attenuation_db']
    if not isinstance(attenuation,(int,float)) or not math.isfinite(attenuation) or not 20<=attenuation<=40 or abs(attenuation*20-round(attenuation*20))>1e-6:
        raise ValueError('USB_RADIO_ATTENUATION')
    if any(str(ch) not in ('1','2') or not isinstance(gain,(int,float)) or not math.isfinite(gain) or not -3<=gain<=30
           for ch,gain in plan['rx_gain_db'].items()):raise ValueError('USB_RADIO_RX_GAIN')


def original(directory):
    """Accept RF originals only from current source, same boot and unchanged profile state; reject foreign paths."""
    c,_,_=radio.modules();path=Path(directory)/'rf-original.json';c.private(path)
    record=c.read_json(path)
    from h102_runtime_usb_prepare import source_manifest
    if record['identity']!=c.identity() or record['sources']!=source_manifest() or record['plan']!=PLAN:
        raise RuntimeError('USB_RF_ORIGINAL_IDENTITY_OR_SOURCE')
    if hashlib.sha256(c.STATE.read_bytes()).hexdigest()!=record['state_sha256']:raise RuntimeError('USB_RF_PROFILE_STATE_CHANGED')
    return record


def prepare(hw,board,directory,launch):
    """Save originals under the manager's three locks; keep RF/data off after configuration before freezing templates."""
    c,rx,_=radio.modules();validate(PLAN)
    if PLAN['configuration']!=launch['profile_id']:raise ValueError('USB_RF_PROFILE_PLAN')
    hw.idle();rx.stopped_tx(hw)
    from h102_runtime_usb_prepare import held_locks,source_manifest
    from h102_rf_deadline import ProcessPin
    from h102_rf_deadline_handshake import pin_value
    locks=held_locks();mapping=radio.describe(hw,c.read_json(c.STATE),c.CATALOG)
    if any(hw.read(hw.phy/f'in_voltage{i}_gain_control_mode')!='spi' for i in (0,1)):
        raise ValueError('USB_RADIO_REQUIRES_MANUAL_SPI_RX_BASELINE')
    for group in mapping['groups']:
        if set(group['channels']).intersection(PLAN['rx_channels']) and set(group['tx_shared']).intersection(PLAN['tx_channels']) and PLAN['rx_frequency']!=PLAN['tx_frequency']:
            raise ValueError('RX_TX_SHARED_LO_FREQUENCY_MISMATCH')
    record=dict(identity=c.identity(),sources=source_manifest(),plan=PLAN,manager=pin_value(ProcessPin.read(__import__('os').getpid())),
        manager_locks=locks,state_sha256=hashlib.sha256(c.STATE.read_bytes()).hexdigest(),settings=hw.settings(),
        tx_modes={str(i):hw.read(hw.phy/f'out_voltage{i}_ensm_mode') for i in (0,1)})
    radio.save(Path(directory)/'rf-original.json',record);deadline=time.monotonic()+25
    def check(calibrated=False):
        if time.monotonic()>deadline:raise TimeoutError('USB_RADIO_PREPARE_DEADLINE')
        hw.verify(complete=False)
        if held_locks()!=locks:raise RuntimeError('USB_RF_MANAGER_LOCK_CHANGED')
        if calibrated:hw.calibration_idle()
        else:hw.idle();rx.stopped_tx(hw)
    try:
        for group in mapping['groups']:
            selected_rx=set(group['channels']).intersection(PLAN['rx_channels'])
            selected_tx=set(group['tx_shared']).intersection(PLAN['tx_channels'])
            if not (selected_rx or selected_tx):continue
            if not group['editable']:raise ValueError('USB_RADIO_LO_GROUP_NOT_EDITABLE')
            frequency=PLAN['rx_frequency'] if selected_rx else PLAN['tx_frequency']
            before=hw.settings()
            if any(int(before[str(hw.alt('adrv9002-phy',ch-1,'frequency'))])!=frequency for ch in group['channels']):
                radio.apply_carrier(hw,before,group['channels'],frequency,check,tx_channels=group['tx_shared'])
        if PLAN['tx_channels']:
            for i in (0,1):
                check();hw.write(hw.phy/f'out_voltage{i}_ensm_mode','calibrated')
                hw.write(hw.phy/f'out_voltage{i}_atten_control_mode','spi')
            for i in (0,1):hw.write(hw.phy/f'out_voltage{i}_en','1')
            mapping=radio.describe(hw,c.read_json(c.STATE),c.CATALOG)['mapping']
            selected_los={mapping[f'tx{ch}LoSelect'] for ch in PLAN['tx_channels']}
            affected=[ch for ch in (1,2) if mapping[f'tx{ch}LoSelect'] in selected_los]
            common.apply_tx_carrier(hw,mapping,affected,PLAN['tx_frequency'])
            for i in (0,1):
                hw.write(hw.phy/f'out_voltage{i}_ensm_mode','primed')
                path=hw.phy/f'out_voltage{i}_hardwaregain'
                gain=-PLAN['attenuation_db'] if i+1 in PLAN['tx_channels'] else float(record['settings'][str(path)].split()[0])
                hw.write(path,str(gain),.001)
        for i in (0,1):
            hw.write(hw.phy/f'in_voltage{i}_ensm_mode','rf_enabled')
            path=hw.phy/f'in_voltage{i}_hardwaregain'
            gain=PLAN['rx_gain_db'].get(str(i+1),record['settings'][str(path)].split()[0])
            hw.write(path,str(gain),.001)
            path=hw.phy/f'in_voltage{i}_ensm_mode';hw.write(path,record['settings'][str(path)])
    finally:
        errors=hw.stop()
        if errors:raise RuntimeError('USB_RADIO_QUIET_STOP:'+repr(errors))
    check()
    prepared=dict(settings=hw.settings(),radio=radio.describe(hw,c.read_json(c.STATE),c.CATALOG),
        tx_modes={str(i):hw.read(hw.phy/f'out_voltage{i}_ensm_mode') for i in (0,1)})
    radio.save(Path(directory)/'rf-prepared.json',prepared)


def sources_match(hw):
    """Allow planned TX RF only; shared peer stays PRIMED; DDS/all DMA must be off."""
    for i in (0,1):
        mode='rf_enabled' if i+1 in PLAN['tx_channels'] else 'primed'
        if hw.read(hw.phy/f'out_voltage{i}_en')!='1' or hw.read(hw.phy/f'out_voltage{i}_ensm_mode')!=mode:return False
    paths=[hw.alt(name,i,'raw') for name in common.DDS for i in range(4)]
    paths += [hw.devices[name]/'buffer/enable' for name in ('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc',*common.DDS)]
    return all(hw.read(path)=='0' for path in paths)


def activate(board):
    """Enable RF after actor template/GNSS preparation without generating/submitting substitute IQ."""
    if not PLAN['rf_enabled']:return
    hw=board.hw;c,_,_=radio.modules();record=original(hw.directory)
    prepared=c.read_json(hw.directory/'rf-prepared.json');hw.verify(complete=False)
    if hw.settings()!=prepared['settings']:raise RuntimeError('USB_RF_PREPARED_SETTINGS_CHANGED')
    try:
        for i in (0,1):
            hw.write(hw.phy/f'out_voltage{i}_ensm_mode','calibrated')
            hw.write(hw.phy/f'out_voltage{i}_atten_control_mode','spi')
        for i in (0,1):hw.write(hw.phy/f'out_voltage{i}_en','1')
        for i in (0,1):
            hw.write(hw.phy/f'in_voltage{i}_ensm_mode','rf_enabled')
            path=hw.phy/f'in_voltage{i}_hardwaregain';hw.write(path,prepared['settings'][str(path)].split()[0],.001)
            path=hw.phy/f'in_voltage{i}_ensm_mode';hw.write(path,prepared['settings'][str(path)])
        for i in (0,1):
            hw.write(hw.phy/f'out_voltage{i}_ensm_mode','primed')
            path=hw.phy/f'out_voltage{i}_hardwaregain';hw.write(path,prepared['settings'][str(path)].split()[0],.001)
        for ch in PLAN['tx_channels']:
            hw.write(hw.phy/f'out_voltage{ch-1}_ensm_mode','rf_enabled')
            # Before ARM/PLAY, DDS/DMA stay off; cross an analog attenuation range once, then restore target.
            # Follow verified GUI handling to avoid correct cached readback with unchanged physical attenuation after powerup.
            hw.write(hw.phy/f'out_voltage{ch-1}_hardwaregain','-20' if PLAN['attenuation_db']>30 else '-40',.001)
            hw.write(hw.phy/f'out_voltage{ch-1}_hardwaregain',str(-PLAN['attenuation_db']),.001)
        hw._gr_radio_active=True
        if not sources_match(hw) or hw.settings()!=prepared['settings']:raise RuntimeError('USB_RF_ACTIVE_READBACK')
        radio.save(hw.directory/'rf-active.json',dict(RF_TX=True,plan=PLAN,radio=prepared['radio'],settings=hw.settings()))
    except BaseException:
        emergency_stop(hw);raise


def emergency_stop(hw):
    """Power down first in exception finally; perform expensive full hashing/recovery afterward without re-enabling RF."""
    if not (hw.directory/'rf-original.json').exists():return
    hw.verify(complete=False);errors=hw.stop();hw._gr_radio_active=False
    if errors:raise RuntimeError('USB_RF_IMMEDIATE_STOP:'+repr(errors))


def fast_stop(directory,request):
    """Independent post checks boot/API/exact lock ownership, powers down, then hashes all startup files.

    Reuse only this request's fully verified RF responsibility; do not delay RF shutdown by rereading tens of MiB.
    The caller already checked source_manifest and holds all three RecoveryManager locks.
    """
    if not (Path(directory)/'rf-original.json').exists():return
    c,_,_=radio.modules();record=original(directory)
    if record['identity']!=request['identity'] or record['manager']!=request['manager']:
        raise RuntimeError('USB_RF_FAST_STOP_OWNERSHIP')
    class StopHardware(c.rf.Hardware):
        """Use lightweight identity checks only for immediate RF shutdown; later open_board retains full SHA/ABI checks."""
        def verify(self,complete=True):return super().verify(complete=False)
    hw=StopHardware(Path(directory),record['identity'],None)
    if (Path('/sys/kernel/debug/iio')/hw.phy.name/'api_version').read_text().strip()!=c.API:
        raise ValueError('USB_RF_FAST_STOP_API')
    emergency_stop(hw)
    radio.save(Path(directory)/'rf-fast-stop.json',dict(RF_TX=False,boot_id=record['identity']['boot_id'],
        immediate_stop_before_full_sha=True))


def restore(hw,board,directory):
    """Restore RF settings after outer-post holds three recovery locks and clears digital/time responsibilities."""
    c,rx,_=radio.modules();record=original(directory);hw.idle();rx.stopped_tx(hw)
    if any(board.read(offset) for offset in (0x200,0x404,0x604,0x620)):raise RuntimeError('USB_RF_RESTORE_DATA_ACTIVE')
    errors=common.settings_restore(hw,record['settings'],record['tx_modes'])
    hw.idle();rx.stopped_tx(hw)
    restored=hw.settings()==record['settings'] and all(hw.read(hw.phy/f'out_voltage{i}_ensm_mode')==mode for i,mode in record['tx_modes'].items())
    result=dict(passed=not errors and restored,settings_restored=restored,errors=errors,RF_TX=False)
    radio.save(Path(directory)/'rf-post.json',result)
    if not result['passed']:raise RuntimeError('USB_RF_RESTORE_FAILED')
    return result


def ready(session):
    """Return actual silent-preparation readback; the same actor independently checks RF and data stages."""
    hw=session.profile._hw;c,_,_=radio.modules()
    return dict(radio=radio.describe(hw,json.loads(session.profile._state_raw),c.CATALOG),
        rx_metadata_capability=session.board.read(0x424),
        rf_requested=PLAN['rf_enabled'],rx_gain_readback={str(i+1):hw.read(hw.phy/f'in_voltage{i}_hardwaregain') for i in (0,1)},
        tx_gain_readback={str(i+1):hw.read(hw.phy/f'out_voltage{i}_hardwaregain') for i in (0,1)})

# End of file: board_usb_radio.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
