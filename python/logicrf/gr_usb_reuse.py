"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file gr_usb_reuse.py
@brief Create new USB RX generations in the same ownership/GNSS context and independently recover all responsibilities.
@details Accept generation preparation after board STOP only; new DigitalSession still requires actual host READY.
         Keep request/ready/initial originals immutable; persist current/pending ledgers before writes. post accepts
         issued values only and rejects unknown state. Bound control JSON; this module does not process IQ.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import hashlib
import base64
import ctypes
import os
import stat
import json
from pathlib import Path
import secrets
import struct
import time

import h102_profile_config as profiles
import h102_rx_v3_config as rx
import h102_tx_v3_config as tx
from h102_rf_usb_owner import allocate_epoch
from h102_rf_usb_tx_storage import atomic_save
from h102_runtime_profile import RuntimeProfile, CONFIG_OFFSETS, TX_OFFSETS
from h102_runtime_rates import NativeTimeWindow
from h102_runtime_usb_session import DigitalPreparation, DigitalSession, _image
import h102_runtime_usb_recovery as legacy_recovery
import board_tx as common
from board_tx import radio


# ABI3 permits these nine writes only; previous templates remain read-only; verify complete images after commit.
METADATA_WORDS = ((0x458, 0x498), (0x450,), (0x454,), (0x46c,), (0x470,),
                  (0x490,), (0x494,), (0x4ac,), (0x4b0,))


def write_metadata(board, offset, value):
    """Write one 32-bit value through the fixed cap3 interface without expanding MMIO or bypassing data interlocks.

    Caller polls asynchronous COMMIT/CANCEL within bounds. Cancel/error-clear requires sources/G0/RX/TX all stopped;
    other commands also require valid prior time/no sticky error. Check old-bank readback before every field write.
    """
    field = type(offset) is int and offset % 4 == 0 and 0x4c0 <= offset <= 0x4e0
    control = type(offset) is int and offset == 0x428 and type(value) is int and value in (1, 2, 4, 8)
    if (type(value) is not int or not 0 <= value < 2**32 or not (field or control)):
        raise ValueError('USB_METADATA_WRITE_OUTSIDE_ABI')
    board.validate()
    if board.read(0x424) != 3:
        raise RuntimeError('USB_METADATA_CAPABILITY')
    if (not board.sources_idle() or board.read(0x30c)&9 or
            any(board.read(o) for o in (0x200, 0x20c, 0x604, 0x60c, 0x620))):
        raise RuntimeError('USB_METADATA_DATA_ACTIVE')
    state = board.read(0x42c)
    if not control or value in (1, 2):
        if board.read(0x404) != 1 or board.read(0x420)&7 != 1 or board.read(0x40c) or state&8:
            raise RuntimeError('USB_METADATA_TIME_OR_ERROR')
        if field and (state&3 != 1 or state&0x70):
            raise RuntimeError('USB_METADATA_NOT_OPEN')
        if control and ((value == 1 and state&3) or (value == 2 and
                (state&3 != 1 or board.read(0x430) != 0x1ff))):
            raise RuntimeError('USB_METADATA_CONTROL_STATE')
    elif value == 8 and (board.read(0x404) or state&0x73):
        raise RuntimeError('USB_METADATA_CLEAR_NOT_DRAINED')
    ctypes.c_uint32.from_buffer(board.memory, offset).value = value
    if field:
        index = (offset-0x4c0)//4
        if (any(board.read(o) != value for o in METADATA_WORDS[index]) or
                not board.read(0x430)&(1 << index)):
            raise RuntimeError('USB_METADATA_FIELD_READBACK')


def quiesce_metadata(board, directory):
    """Independent post drains issued ledger transactions only; no template restore/error clear before late ACKs return to zero.

    Normal commit leaves REQ high; complete four-phase CANCEL to return to zero. Reject unregistered activity/unknown
    zero writes; missing clock fails within bounds while retaining responsibility. Disable time here, not restore GNSS.
    """
    if board.read(0x424) != 3:return
    state = board.read(0x42c)
    if state&~0xff:raise RuntimeError('USB_METADATA_RECOVERY_UNKNOWN_STATUS')
    if not state&0x7f:return
    ledger = _load(directory)
    transaction = ledger.get('metadata_transaction') if ledger else None
    if (not transaction or set(transaction) != {'schema','abi','configuration_id',
            'previous_configuration_id','time_epoch','phase'} or transaction.get('schema') != 1 or
            transaction.get('abi') != 1 or transaction.get('phase') not in ('intent','committed') or
            type(transaction.get('previous_configuration_id')) is not int or
            not 0 < transaction['previous_configuration_id'] < transaction.get('configuration_id',0) or
            transaction.get('time_epoch') != board.read(0x408) or
            transaction.get('configuration_id') not in {
                row['prepared']['configuration_id'] for row in
                [ledger['current']] + ([ledger['pending']] if ledger['pending'] else [])}):
        raise RuntimeError('USB_METADATA_RECOVERY_FOREIGN_TRANSACTION')
    board.write_metadata(0x428, 4)
    deadline = time.monotonic()+.75
    while board.read(0x42c)&0x73:
        board.validate()
        if time.monotonic() >= deadline:
            raise TimeoutError('USB_METADATA_CANCEL_ACK_NOT_DRAINED')
        time.sleep(.001)
    board.write_time(0x404, 0)
    while board.read(0x420)&0x18 != 0x18:
        if time.monotonic() >= deadline:raise TimeoutError('USB_METADATA_RECOVERY_MAILBOX')
        time.sleep(.001)
    board.write_metadata(0x428, 8)
    if board.read(0x40c) or board.read(0x42c)&0x7f:
        raise RuntimeError('USB_METADATA_RECOVERY_NOT_CLEAR')
    ledger['metadata_transaction'] = None
    atomic_save(Path(directory)/'reuse-digital.json', ledger)


def _request_sha(directory):
    return hashlib.sha256((directory/'request.json').read_bytes()).hexdigest()


def _read(directory, name):
    path = Path(directory)/name
    profiles.private(path)
    return profiles.read_json(path)


def _entry(prepared):
    return dict(prepared=prepared.record(), rx_hex=prepared.rx_native.hex(),
                tx_hex=prepared.tx_templates.hex())


def _load(directory):
    """Bind private responsibility ledgers to the initial immutable request; missing ledgers use legacy one-shot recovery."""
    directory = Path(directory)
    if not (directory/'reuse-digital.json').exists():
        return None
    value = _read(directory, 'reuse-digital.json')
    if (value['schema'] != 1 or value['request_sha256'] != _request_sha(directory) or
            value['identity'] != profiles.identity()):
        raise RuntimeError('USB_REUSE_LEDGER_IDENTITY')
    return value


def _targets(board, original, prepared):
    """Verify originals and all issued templates before returning allowed values per register."""
    old_rx, old_tx = legacy_recovery.validate_original(original, prepared)
    ledger = _load(board.hw.directory)
    if ledger is None:
        return None
    entries = [ledger['current']]+([ledger['pending']] if ledger['pending'] else [])
    images = []
    for entry in entries:
        p = entry['prepared'];a = bytes.fromhex(entry['rx_hex']);b = bytes.fromhex(entry['tx_hex'])
        if (len(a) != 112 or len(b) != 256 or p['tx_mask'] or p['rx_mask'] != prepared.rx_mask or
                p['device_id'] != str(prepared.device_id) or p['configuration_id'] < prepared.epoch or
                hashlib.sha256(a).hexdigest() != p['rx_sha256'] or
                hashlib.sha256(b).hexdigest() != p['tx_sha256']):
            raise RuntimeError('USB_REUSE_LEDGER_TEMPLATE')
        images.append((p, a, b))
    targets = [(0x204, original['rx_epoch'], {original['rx_epoch']} |
                {p['configuration_id'] for p, _, _ in images}),
               (0x618, original['seed'], {original['seed']}),
               (0x61c, original['checker'], {original['checker']})]
    for offsets, old, index in ((CONFIG_OFFSETS, old_rx, 1), (TX_OFFSETS, old_tx, 2)):
        rows = [struct.unpack('<'+str(len(offsets))+'I', row[index]) for row in images]
        for n, (offset, initial) in enumerate(zip(offsets, struct.unpack('<'+str(len(offsets))+'I', old))):
            targets.append((offset, initial, {initial}|{row[n] for row in rows}))
    if any(board.read(offset) not in allowed for offset, _, allowed in targets):
        raise RuntimeError('USB_REUSE_RECOVERY_FOREIGN_WORD')
    return targets, images


def stop_configuration(board, original, prepared):
    """Stop only complete active generations or stopped partial commits; never overwrite external control/values."""
    targets = _targets(board, original, prepared)
    if targets is None:
        return legacy_recovery.stop_configuration(board, original, prepared)
    board.validate()
    if any(board.read(o) for o in (0x604, 0x620)):
        raise RuntimeError('USB_REUSE_RECOVERY_EXTERNAL_TX')
    control = board.read(0x200)
    if control and (control != 0x80000000|prepared.rx_mask or not any(
            board.read(0x204) == p['configuration_id'] and
            _image(board, CONFIG_OFFSETS) == a and _image(board, TX_OFFSETS) == b
            for p, a, b in targets[1])):
        raise RuntimeError('USB_REUSE_RECOVERY_ACTIVE_FOREIGN')
    board.write(0x200, 0)
    if board.read(0x200):
        raise RuntimeError('USB_REUSE_RECOVERY_STOP_READBACK')
    quiesce_metadata(board, board.hw.directory)
    return []


def restore_configuration(board, original, prepared):
    """Restore original digital values after time shutdown; prevalidate the full table, then recheck/write each word."""
    legacy_recovery._idle(board)
    targets = _targets(board, original, prepared)
    if targets is None:
        return legacy_recovery.restore_configuration(board, original, prepared)
    for offset, initial, allowed in reversed(targets[0]):
        legacy_recovery._idle(board)
        if board.read(offset) not in allowed:
            raise RuntimeError('USB_REUSE_RECOVERY_WORD_CHANGED')
        board.write(offset, initial)
        if board.read(offset) != initial:
            raise RuntimeError('USB_REUSE_RECOVERY_READBACK')
    legacy_recovery._idle(board)
    return dict(rx_epoch=board.read(0x204), seed=board.read(0x618), checker=board.read(0x61c),
                rx_hex=_image(board, CONFIG_OFFSETS).hex(), tx_hex=_image(board, TX_OFFSETS).hex())


def owns_time(board, directory, original_epoch):
    """Independent post accepts initial/persistently registered time epochs only, without guessing from arbitrary state files."""
    ledger = _load(directory)
    allowed = {original_epoch}
    if ledger is not None:
        allowed.update(v for v in (ledger.get('current_time_epoch'), ledger.get('pending_time_epoch'))
                       if type(v) is int and v > 0)
    state = _read(directory, 'state.json')
    return board.read(0x408) in allowed and board.read(0x408) == state.get('gnss_allocated_epoch')


def restore_initial_radio(hw, directory, request):
    """Restore initial silent preparation in independent post; outer-post then restores the user's original RF state."""
    if not (Path(directory)/'reuse-radio.json').exists():
        return
    record = _read(directory, 'reuse-radio.json')
    if record['request_sha256'] != _request_sha(Path(directory)):
        raise RuntimeError('USB_REUSE_RADIO_REQUEST_CHANGED')
    pending = record['pending'];current = hw.settings()
    if pending:
        radio.validate_restore_scene(hw, pending['before'], current, pending['channels'],
            pending['frequency_hz'], failed=pending.get('failed'), tx_channels=pending['tx_shared'])
    elif current != record['active']:
        raise RuntimeError('USB_REUSE_RADIO_FOREIGN_SETTINGS')
    hw.idle()
    initial = _read(directory, 'rf-prepared.json')
    errors = common.settings_restore(hw, request['settings'], initial['tx_modes'])
    hw.idle()
    if errors or hw.settings() != request['settings']:
        raise RuntimeError('USB_REUSE_RADIO_RESTORE_FAILED:'+repr(errors))
    atomic_save(Path(directory)/'reuse-radio-post.json', dict(passed=True, settings_restored=True), exclusive=True)


class ReusableRxSession:
    """Single-thread RX-only digital sessions retain strict cross-generation sequence; never resurrect old sessions.

    All control enters serially through the private FIFO with real short-lease checks. Generation preparation never
    auto-renews; timeout/drift invokes independent post. Retain original time counters and report requalification gaps.
    """
    def __init__(self, session, directory, request):
        if session.prepared.tx_mask or not session.prepared.rx_mask:
            raise ValueError('USB_REUSE_RX_ONLY')
        self.current = session
        self.directory = Path(directory)
        self.request = request
        self.token = session.token
        self.sequence = 0
        self.epoch = 0
        self.preparing = None
        self.epoch_info = None
        self.ledger = dict(schema=1, request_sha256=_request_sha(self.directory),
            identity=profiles.identity(), current=_entry(session.prepared), pending=None,
            current_time_epoch=session.time_epoch, pending_time_epoch=None)
        self.radio_record = dict(request_sha256=_request_sha(self.directory),
                                 active=session.profile._hw.settings(), pending=None)
        atomic_save(self.directory/'reuse-digital.json', self.ledger, exclusive=True)
        atomic_save(self.directory/'reuse-radio.json', self.radio_record, exclusive=True)

    def __getattr__(self, name):
        return getattr(self.current, name)

    @property
    def closed(self):
        return self.preparing is None and self.current.closed

    @property
    def state(self):
        return 'PREPARING_EPOCH' if self.preparing is not None else self.current.state

    def check(self):
        """Preparation retains two-second client leases/eight-second absolute deadline without background renewal."""
        phase = self.preparing
        if phase is None:
            return self.current.check()
        now = self.current.clock()
        if now >= phase['expires'] or now >= phase['deadline']:
            raise TimeoutError('USB_REUSE_PREPARATION_CLOSED_OR_EXPIRED')
        if phase['stage'] == 'building':
            try:
                next(phase['steps'])
            except StopIteration:
                if phase['stage'] == 'retained':
                    self.current = phase['session']
                    self.preparing = None
                    return
                if phase['stage'] != 'acquiring':raise RuntimeError('USB_REUSE_BUILD_WITHOUT_ACQUISITION')
            if phase['stage'] == 'building':return
        self._preparing_hardware()
        try:
            phase['window'].observe(phase['board'].snapshot(0, phase['prepared'].epoch))
        except RuntimeError as error:
            if (not phase['window'].record()['failed'] and str(error) in
                    ('NATIVE_RAW_EPOCH_OR_SOURCE', 'NATIVE_TIME_OR_RUNTIME_RATE_NOT_QUALIFIED')):
                return
            raise
        current = DigitalSession(phase['profile'], phase['board'], phase['prepared'],
                                 self.request['seconds'], phase['window'], clock=self.current.clock)
        # Eligibility alone does not extend leases; subsequent valid commands may renew two seconds.
        current.expires = phase['expires']
        self.current = current
        self.preparing = None
        self.epoch_info['native_time'] = current.time_window.record()
        self.epoch_info['preparation_steps_seconds']['qualified']=current.clock()-phase['started']

    def _preparing_hardware(self):
        """Verify every control/template during time acquisition; no early DATA or committed-parameter changes."""
        phase = self.preparing;board = phase['board'];prepared = phase['prepared']
        phase['profile'].check();board.validate()
        if (not board.sources_idle() or any(board.read(o) for o in
                (0x200, 0x20c, 0x40c, 0x604, 0x60c, 0x620)) or
                board.read(0x404) != 1 or board.read(0x408) != prepared.epoch or
                board.read(0x204) != prepared.epoch or
                _image(board, CONFIG_OFFSETS) != prepared.rx_native or
                _image(board, TX_OFFSETS) != prepared.tx_templates):
            raise RuntimeError('USB_REUSE_PREPARING_HARDWARE_CHANGED')

    def close(self):
        """Keep data off during preparation; check the complete new template before independent post handles time/originals."""
        if self.preparing is not None:
            if self.preparing['stage'] == 'acquiring':
                self._preparing_hardware()
            else:
                board=self.current.board;board.validate()
                if not board.sources_idle() or any(board.read(o) for o in (0x200,0x604,0x620)):
                    raise RuntimeError('USB_REUSE_BUILD_CLOSE_NOT_IDLE')
            self.preparing = None
        self.current.close()

    def _audit(self, request):
        """Read logs only in actual STOPPED state; atomically clear persistently acknowledged segments, retaining recovery ledgers.

        Current main is the sole writer. Crashed audit-current.bin/pending remain for full archive after independent post;
        cursors never replace originals. Each open is bounded to 2 MiB and chunks to 12 KiB.
        """
        if self.preparing is not None or self.current.state!='STOPPED' or self.current.closed:
            raise RuntimeError('USB_AUDIT_REQUIRES_STOPPED')
        board=self.current.board
        if not board.sources_idle() or any(board.read(o) for o in (0x200,0x604,0x620)):
            raise RuntimeError('USB_AUDIT_DATA_ACTIVE')
        directory=self.directory;source=directory/'writes.jsonl'
        pending=directory/'audit-current.json';copy=directory/'audit-current.bin'
        operation=request['op'];base={'op','token','seq'}
        fields=base | (set() if operation=='audit_open' else {'audit_id','index'} if operation=='audit_chunk' else {'audit_id','sha256'})
        if set(request)!=fields:raise ValueError('USB_AUDIT_FIELDS')
        def image(path):
            profiles.private(path)
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2*1024*1024:
                raise RuntimeError('USB_AUDIT_FILE')
            return path.read_bytes()
        if operation=='audit_open' and not pending.exists():
            raw=image(source);cursor=directory/'audit-cursor.json'
            previous=_read(directory,'audit-cursor.json') if cursor.exists() else None
            descriptor=dict(id=secrets.token_hex(16),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
                epoch=self.epoch,request_sha256=_request_sha(directory),identity=profiles.identity(),
                previous_sha256=previous['sha256'] if previous else None,
                segments=(previous['segments'] if previous else 0)+1)
            with copy.open('xb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            atomic_save(pending,descriptor,exclusive=True)
        descriptor=_read(directory,'audit-current.json');raw=image(copy)
        if (descriptor['request_sha256']!=_request_sha(directory) or descriptor['identity']!=profiles.identity() or
                hashlib.sha256(raw).hexdigest()!=descriptor['sha256'] or len(raw)!=descriptor['bytes']):
            raise RuntimeError('USB_AUDIT_IDENTITY')
        if operation=='audit_open':
            # One bounded 12 KiB block can accompany the descriptor; the host
            # still persists and verifies the full SHA before a separate retire.
            return dict(descriptor,first_chunk=dict(id=descriptor['id'],index=0,
                data=base64.b64encode(raw[:12288]).decode())) if raw else descriptor
        if request['audit_id']!=descriptor['id']:raise RuntimeError('USB_AUDIT_ID')
        if operation=='audit_chunk':
            index=request['index']
            if type(index) is not int or index<0 or index*12288>=len(raw):
                raise ValueError('USB_AUDIT_CHUNK')
            return dict(id=descriptor['id'],index=index,data=base64.b64encode(raw[index*12288:(index+1)*12288]).decode())
        if request['sha256']!=descriptor['sha256'] or image(source)!=raw:
            raise RuntimeError('USB_AUDIT_ORIGINAL_CHANGED')
        empty=directory/'audit-empty.tmp'
        with empty.open('xb') as output:output.flush();os.fsync(output.fileno())
        os.replace(empty,source)
        receipt=dict(descriptor,status='RETIRED_EXACT_VERIFIED')
        atomic_save(directory/'audit-cursor.json',receipt)
        copy.unlink();pending.unlink()
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return receipt

    def command(self, request):
        """Forward legacy commands with strict outer sequence; new generations require STOPPED and matching old stream ID."""
        try:
            # Active DigitalSession.command performs full before/after checks; stepwise check validates preparation.
            if self.preparing is not None:self.check()
            if (not isinstance(request, dict) or not isinstance(request.get('token'), str) or
                    not secrets.compare_digest(request['token'], self.token) or
                    type(request.get('seq')) is not int or request['seq'] != self.sequence+1):
                raise RuntimeError('USB_REUSE_COMMAND_IDENTITY')
            if self.preparing is not None:
                if set(request) != {'op','token','seq'} or request['op'] not in ('renew','snapshot','close','stop'):
                    raise RuntimeError('USB_REUSE_PREPARATION_NOT_READY')
                if request['op'] in ('close','stop'):
                    self.close()
                elif request['op']=='renew':
                    self.preparing['expires'] = self.current.clock()+2
                reply = dict(ok=True,state=self.state,native=None,rx_control=0,tx_control=0)
            elif request.get('op') in ('audit_open','audit_chunk','audit_retire'):
                reply=self.current.command(dict(op='renew',token=self.current.token,seq=self.current.sequence+1))
                reply['audit']=self._audit(request)
            elif request.get('op') == 'receiver_ready_start':
                if set(request)!={'op','token','seq','stream_id','configuration_sha256'}:
                    raise ValueError('USB_REUSE_READY_START_FIELDS')
                if not self.current.prepared.rx_mask or self.current.prepared.tx_mask:
                    raise RuntimeError('USB_REUSE_READY_START_RX_ONLY')
                # One native command keeps complete actual checks at both
                # transaction boundaries, binding and time before DATA. There is
                # no network wait or hardware write between binding and START.
                reply=self.current.command(dict(op='receiver_ready_start',token=self.current.token,
                    seq=self.current.sequence+1,stream_id=request['stream_id'],
                    configuration_sha256=request['configuration_sha256']))
            elif request.get('op') == 'prepare_epoch':
                if set(request) != {'op', 'token', 'seq', 'frequency_hz', 'previous_stream_id'}:
                    raise ValueError('USB_REUSE_EPOCH_FIELDS')
                if (self.current.state != 'STOPPED' or self.current.closed or
                        request['previous_stream_id'] != str(self.prepared.rx_stream_id)):
                    raise RuntimeError('USB_REUSE_REQUIRES_STOPPED_MATCHING_STREAM')
                self.current.check()
                self.current.expires = self.current.clock()+2
                self.preparing = dict(stage='building',expires=self.current.expires,
                    deadline=self.current.clock()+8,steps=self._prepare_epoch(request['frequency_hz']))
                self.check()
                reply = dict(ok=True,state=self.state,native=None,rx_control=0,tx_control=0)
            else:
                reply = self.current.command(dict(request, token=self.current.token, seq=self.current.sequence+1))
            self.sequence = request['seq']
            reply['seq'] = self.sequence
            # Only the preparation boundary publishes the large immutable
            # configuration/time proof. Running snapshots and audit replies
            # retain their live native evidence without resending that proof.
            if (self.epoch_info is not None and (reply.get('state')=='CONFIGURED' or
                    reply.get('state')=='PREPARING_EPOCH' and self.preparing['stage']=='acquiring') and
                    request['op'] not in ('audit_open','audit_chunk','audit_retire')):
                reply['epoch_info']=self.epoch_info
            return reply
        except BaseException:
            self.close()
            raise

    def _prepare_epoch(self, frequency):
        """Verify same-package LO while stopped and create a new object; cap3 retains actual time, other paths reacquire it."""
        if type(frequency) is not int or not 50_000_000 <= frequency <= 5_800_000_000:
            raise ValueError('USB_REUSE_FREQUENCY')
        previous = self.current;board = previous.board;hw = previous.profile._hw
        started=previous.clock();timings={}
        c, rx_owner, _ = radio.modules()
        def check(calibrated=False):
            check_started=previous.clock()
            if (previous.clock() >= self.preparing['expires'] or
                    previous.clock() >= self.preparing['deadline']):
                raise TimeoutError('USB_REUSE_PREPARATION_LEASE_EXPIRED')
            # The pinned DigitalBoard.validate already verifies boot/machine/MAC.
            # sources_idle reads both TX enables, all DDS raw values and all DMA
            # enables. Combined with these live MMIO checks it covers precisely
            # DigitalHardware.idle + stopped_tx, without reading the same RF
            # attributes three times within this one serial preparation step.
            board.validate()
            if (any(board.read(o) for o in (0x200, 0x20c, 0x40c, 0x604, 0x60c, 0x620)) or
                    board.read(0x30c)&9):
                raise RuntimeError('USB_REUSE_PREPARATION_DATA_ACTIVE')
            if not board.sources_idle():raise RuntimeError('USB_REUSE_SOURCE_ACTIVE')
            if calibrated:hw.calibration_idle()
            timings['guard_calls']=timings.get('guard_calls',0)+1
            timings['guard_seconds']=timings.get('guard_seconds',0.)+previous.clock()-check_started
        check();previous._ownership()
        mapping = radio.describe(hw, profiles.read_json(profiles.STATE), c.CATALOG)
        channels = [ch for ch in (1, 2) if previous.prepared.rx_mask & (1 << (ch-1))]
        hot = (board.read(0x424) == 3 and previous.profile.expected_profile in
               ((2000000, 250000), (40000000, 20000000)) and
               mapping.get('initial_calibrations') == 'off' and all(
                   group['editable'] and not group['tx_shared'] for group in mapping['groups']
                   if set(group['channels']).intersection(channels)))
        same_carrier=all(mapping['rx_center_frequency_hz'][str(ch)]==frequency for ch in channels)
        yield
        # RTL forbids RX template writes with time_enable=1. After error-free STOP of the old window,
        # disable time permission and await CDC mailbox zero; never clear sticky faults to hide existing errors.
        previous._native()
        previous_time = previous.time_window.record()
        if not hot:
            quiesce_metadata(board, self.directory)
            self.ledger['metadata_transaction'] = None
            board.write_time(0x404, 0)
            mailbox_deadline = previous.clock()+.5
            while board.read(0x420)&0x18 != 0x18:
                check()
                if previous.clock() >= mailbox_deadline:raise TimeoutError('USB_REUSE_MAILBOX_NOT_IDLE')
                time.sleep(.001)
        yield
        timings['time_retained' if hot else 'time_disabled']=previous.clock()-started
        if hw.settings() != self.radio_record['active']:
            raise RuntimeError('USB_REUSE_SETTINGS_CHANGED')
        for group in mapping['groups']:
            if not set(group['channels']).intersection(channels):continue
            if not group['editable']:raise RuntimeError('USB_REUSE_LO_NOT_EDITABLE')
            if all(mapping['rx_center_frequency_hz'][str(ch)] == frequency for ch in group['channels']):continue
            pending = dict(before=hw.settings(), channels=group['channels'],
                           tx_shared=group['tx_shared'], frequency_hz=frequency)
            self.radio_record['pending'] = pending
            atomic_save(self.directory/'reuse-radio.json', self.radio_record)
            try:
                actual = radio.apply_carrier(hw, pending['before'], group['channels'], frequency,
                                             check, tx_channels=group['tx_shared'])
            except BaseException:
                pending['failed'] = hw.settings()
                atomic_save(self.directory/'reuse-radio.json', self.radio_record)
                raise
            self.radio_record.update(active=actual, pending=None)
            atomic_save(self.directory/'reuse-radio.json', self.radio_record)
            yield
        yield
        check()
        mapping = radio.describe(hw, profiles.read_json(profiles.STATE), c.CATALOG)
        if any(mapping['rx_center_frequency_hz'][str(ch)] != frequency for ch in channels):
            raise RuntimeError('USB_REUSE_CARRIER_READBACK')
        if hot:
            previous.time_window.observe(board.snapshot(0, previous.time_epoch))
        timings['radio_readback']=previous.clock()-started
        if hot and same_carrier:
            # RuntimeProfile is immutable and remains inside the same locked
            # owner scope. templates() below rechecks real state, SSI and all
            # four channel parameters; reuse static input, never cached validity.
            profile = previous.profile
        else:
            package = profiles.load_package(c.CATALOG, self.request['profile_id'], trusted=True)
            profile = RuntimeProfile(hw, board, profiles.STATE, profiles.STATE.read_bytes(), package,
                rx.read_applied(hw.phy, expected_profile=previous.profile.expected_profile),
                tx.read_applied(hw.phy, expected_profile=previous.profile.expected_profile))
        path = Path('/var/lib/h102-rx/time-epoch.json');profiles.private(path)
        epoch = allocate_epoch(max(profile.previous_rx_epoch, profile.previous_time_epoch,
            profiles.read_json(path)['last_epoch'], profiles.read_json(profiles.STATE)['configuration_id']))
        atomic_save(path, dict(last_epoch=epoch))
        stream = secrets.randbits(64) or 1
        tx_stream = secrets.randbits(64) or 2
        if stream == previous.prepared.rx_stream_id or stream == tx_stream:
            raise RuntimeError('USB_REUSE_STREAM_COLLISION')
        native, templates = profile.templates(stream, tx_stream, epoch, previous.prepared.device_id)
        if hot:
            # Keep full TX templates/stream IDs and freeze other RX words; new packages/gain changes are not warm tuning.
            mutable = {4, 5, 6, 11, 12, 18, 19, 20, 25, 26}
            before = struct.unpack('<28I', previous.prepared.rx_native)
            after = struct.unpack('<28I', native)
            if any(a != b for n, (a, b) in enumerate(zip(before, after)) if n not in mutable):
                raise RuntimeError('USB_METADATA_FROZEN_RX_CHANGED')
            templates = previous.prepared.tx_templates
            tx_stream = previous.prepared.tx_stream_id
        prepared = DigitalPreparation(previous.prepared.rx_mask, 0, native, templates, epoch,
            previous.prepared.device_id, stream, tx_stream, previous.prepared.seed)
        self.ledger['pending'] = _entry(prepared)
        self.ledger['pending_time_epoch'] = previous.time_epoch if hot else epoch
        if hot:
            self.ledger['metadata_transaction'] = dict(schema=1, configuration_id=epoch,
                previous_configuration_id=previous.prepared.epoch, time_epoch=previous.time_epoch,
                phase='intent', abi=1)
        atomic_save(self.directory/'reuse-digital.json', self.ledger)
        yield
        timings['templates_computed']=previous.clock()-started
        check();previous.close()
        if hot:
            yield from self._commit_metadata(profile, prepared, mapping, previous_time, started, timings, check)
            return
        changes=[]
        for offsets,raw in ((CONFIG_OFFSETS,native),(TX_OFFSETS,templates)):
            for offset,word in zip(offsets,struct.unpack('<'+str(len(offsets))+'I',raw)):
                old=board.read(offset)
                if old!=word:changes.append((offset,old,word))
        for start in range(0,len(changes),8):
            check()
            # Equal values are read-only; every write still checks identity/ABI, all control interlocks and exact readback.
            for offset,old,word in changes[start:start+8]:
                if previous.clock() >= self.preparing['expires']:
                    raise TimeoutError('USB_REUSE_PREPARATION_LEASE_EXPIRED')
                board.validate()
                if (board.read(0x404) or board.read(0x420)&0x18 != 0x18 or
                        any(board.read(o) for o in (0x200,0x20c,0x40c,0x604,0x60c,0x620))):
                    raise RuntimeError('USB_REUSE_RTL_TEMPLATE_INTERLOCK')
                if board.read(offset)!=old:raise RuntimeError('USB_REUSE_TEMPLATE_WORD_CHANGED')
                board.write(offset,word)
            check()
            yield
        check();board.write(0x204, epoch)
        if (_image(board, CONFIG_OFFSETS) != native or _image(board, TX_OFFSETS) != templates or
                board.read(0x204) != epoch):
            raise RuntimeError('USB_REUSE_TEMPLATE_READBACK')
        check()
        timings['templates_committed']=previous.clock()-started
        baseline = board.snapshot(0, previous.time_epoch)
        window = NativeTimeWindow(baseline, 3, profile.expected_profile, epoch)
        state = _read(self.directory, 'state.json')
        state['gnss_allocated_epoch'] = epoch
        atomic_save(self.directory/'state.json', state)
        board.write_time(0x408, epoch);board.write_time(0x404, 1)
        self.ledger.update(current=_entry(prepared), pending=None,
                           current_time_epoch=epoch, pending_time_epoch=None)
        atomic_save(self.directory/'reuse-digital.json', self.ledger)
        self.preparing.update(stage='acquiring',profile=profile,board=board,prepared=prepared,window=window)
        self.epoch += 1
        timings['time_enabled']=previous.clock()-started
        self.preparing['started']=started
        self.epoch_info = dict(preparation_steps_seconds=timings,template_total_words=92,template_changed_words=len(changes),prepared=prepared.record(), radio=mapping, configuration_hex=native.hex(),
            time_epoch=epoch, epoch=self.epoch, previous_stopped_not_restarted=True,
            previous_time=previous_time, acquisition_baseline=baseline, native_time=None,
            time_reacquired_after_rtl_template_interlock=True)
        return

    def _commit_metadata(self, profile, prepared, mapping, previous_time, started, timings, check):
        """Publish only after persisted pre-write ledgers, actual dual ACK/templates/global ID and continuous NativeTimeWindow checks.

        Each generator step lets the original FIFO process explicit renewals, without background renewal. Exceptions preserve
        the old session; independent post uses the same ledger to drain the four-phase handshake and recover.
        """
        previous = self.current;board = previous.board
        window = previous.time_window
        def observe():
            check()
            snapshot_started=previous.clock()
            window.observe(board.snapshot(0, previous.time_epoch))
            timings['snapshot_seconds']=timings.get('snapshot_seconds',0.)+previous.clock()-snapshot_started
        def wait(complete, reason):
            deadline = previous.clock()+.5
            while not complete(board.read(0x42c)):
                observe()
                if board.read(0x42c)&8:raise RuntimeError('USB_METADATA_RTL_FAULT')
                if previous.clock() >= deadline:raise TimeoutError(reason)
                yield
        observe();board.write_metadata(0x428, 1)
        yield from wait(lambda s:s&0x7b == 1, 'USB_METADATA_BEGIN_ACK_TIMEOUT')
        words = struct.unpack('<28I', prepared.rx_native)
        values = (words[6],words[4],words[5],words[11],words[12],
                  words[18],words[19],words[25],words[26])
        if words[6] != words[20] or words[6] != prepared.epoch:
            raise RuntimeError('USB_METADATA_CONFIGURATION_ID')
        # Exactly nine bounded writes form one scheduling slice; every word
        # still performs its own live lease/source/time checks and readback.
        # Avoid dispatching renew replies between fields of this fixed payload.
        for index, value in enumerate(values):
            observe();board.write_metadata(0x4c0+index*4, value)
        yield
        observe();board.write_metadata(0x428, 2)
        yield from wait(lambda s:s&0x7f == 0x74, 'USB_METADATA_DUAL_ACK_TIMEOUT')
        observe()
        if (board.read(0x204) != prepared.epoch or board.read(0x430) != 0x1ff or
                board.read(0x404) != 1 or board.read(0x408) != previous.time_epoch or
                _image(board, CONFIG_OFFSETS) != prepared.rx_native or
                _image(board, TX_OFFSETS) != prepared.tx_templates):
            raise RuntimeError('USB_METADATA_COMMIT_READBACK')
        session = DigitalSession(profile, board, prepared, self.request['seconds'], window, clock=previous.clock)
        session.expires = self.preparing['expires']
        proof = dict(abi=1, capability=3, status=board.read(0x42c), dirty=board.read(0x430),
            configuration_id=board.read(0x204), time_epoch=board.read(0x408),
            previous_observations=previous_time['observations'], observations=window.record()['observations'])
        self.ledger.update(current=_entry(prepared),pending=None,
            current_time_epoch=previous.time_epoch,pending_time_epoch=None)
        self.ledger['metadata_transaction']['phase'] = 'committed'
        atomic_save(self.directory/'reuse-digital.json', self.ledger)
        self.epoch += 1
        timings['qualified'] = previous.clock()-started
        self.epoch_info = dict(preparation_steps_seconds=timings, prepared=prepared.record(),
            radio=mapping, configuration_hex=prepared.rx_native.hex(), time_epoch=previous.time_epoch,
            epoch=self.epoch,previous_stopped_not_restarted=True,previous_time=previous_time,
            native_time=window.record(),metadata_commit=proof,
            time_retained_after_dual_native_metadata_commit=True,
            time_reacquired_after_rtl_template_interlock=False)
        self.preparing.update(stage='retained',session=session)

# End of file: gr_usb_reuse.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
