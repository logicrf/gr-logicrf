"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_reuse_model.py
@brief Model time separation/partial-write recovery using the actual pinned generated USB state machine.
@details Registers/NativeTimeWindow observations are explicit substitutes; actual generated
         DigitalSession/recovery functions are under test, without device access or USB/RF/GNSS hardware qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import gr_usb_reuse as reuse
from h102_runtime_usb_session import DigitalSession, DigitalPreparation
from h102_runtime_profile import CONFIG_OFFSETS, TX_OFFSETS
from h102_runtime_rates import NativeTimeWindow


class Clock:
    """Explicitly advance model time and verify time acquisition never auto-renews permissions."""
    now = 0.
    def __call__(self):return self.now


class Board:
    """Explicit MMIO word model logs writes and checks foreign-word rejection before any partial change."""
    def __init__(self, directory):
        self.words = {0x204: 147, 0x404: 1, 0x408: 145, 0x420: 0x18}
        self.writes = []
        self.hw = SimpleNamespace(directory=directory)
    def validate(self):pass
    def idle(self):pass
    def sources_idle(self):return True
    def read(self, offset):return self.words.get(offset, 0)
    def write(self, offset, value):
        if offset in CONFIG_OFFSETS and self.read(0x404):
            raise AssertionError('RTL template write interlock: time must be disabled')
        self.words[offset] = value;self.writes.append((offset, value))
    def write_time(self, offset, value):
        if offset == 0x408 and self.read(0x404):raise AssertionError('time epoch write while enabled')
        self.words[offset] = value;self.writes.append((offset, value))
    def snapshot(self, mask=0, epoch=None):
        # Match the actual SDK: epoch binds the transport queue, while the
        # timestamp service reports its own independent 0x408 generation.
        if mask:
            assert self.read(0x200)==0x80000000|mask and epoch==self.read(0x204), 'TRANSPORT_OWNER_CHANGED'
        return dict(mask=mask,time_epoch=self.read(0x408))


def image(board, offsets, raw):
    """Write exact template words in the model, not actual hardware commits."""
    for offset, value in zip(offsets, struct.unpack('<'+str(len(offsets))+'I', raw)):
        board.words[offset] = value


def fixture(directory):
    """Construct actual DigitalSession with distinct configuration147/time145."""
    board = Board(directory)
    prepared = DigitalPreparation(3, 0, bytes([1])*112, bytes([2])*256, 147, 1234, 41, 42, 0)
    image(board, CONFIG_OFFSETS, prepared.rx_native);image(board, TX_OFFSETS, prepared.tx_templates)
    window = NativeTimeWindow.__new__(NativeTimeWindow)
    board.hw.settings=lambda:{}
    board.hw.verify=lambda **kwargs:None
    board.hw.idle=lambda:None
    board.hw.calibration_idle=lambda:None
    board.hw.phy=directory/'phy'
    profile = SimpleNamespace(expected_profile=(2000000, 250000), check=lambda:None, _hw=board.hw)
    session = DigitalSession(profile, board, prepared, 0, window, clock=Clock())
    return session, board, prepared


def preparation_interlock_model(directory):
    """Run actual same-frequency prepare_epoch; the model forbids RX writes with time enabled."""
    directory.mkdir()
    for name, value in (('request.json', {}), ('state.json', {'gnss_allocated_epoch':145}),
                        ('profile-state.json', {'configuration_id':1}), ('allocated-time.json', {'last_epoch':147})):
        reuse.atomic_save(directory/name,value)
    session, board, prepared = fixture(directory)
    global_time = Path('/var/lib/h102-rx/time-epoch.json')
    original_read = reuse.profiles.read_json;original_save = reuse.atomic_save
    def read(path):return original_read(directory/'allocated-time.json' if Path(path)==global_time else path)
    def save(path, value, *args, **kwargs):
        return original_save(directory/'allocated-time.json' if Path(path)==global_time else path,value,*args,**kwargs)
    class Profile:
        expected_profile=(2000000,250000)
        def __init__(self, hw, board, *args):
            self._hw=hw;self.board=board;self.previous_rx_epoch=board.read(0x204);self.previous_time_epoch=board.read(0x408)
        def check(self):pass
        def templates(self,*args):
            a=bytearray(reuse._image(self.board,CONFIG_OFFSETS));b=bytearray(reuse._image(self.board,TX_OFFSETS))
            a[8:12]=(148).to_bytes(4,'little');b[12:16]=(148).to_bytes(4,'little')
            return bytes(a),bytes(b)
    def window_init(self, baseline, mask, profile, epoch):
        self.model=dict(observations=0,failed=False,mask=mask,time_epoch=epoch,expected_profile=list(profile))
    def window_record(self):
        return getattr(self,'model',dict(observations=1,failed=False,mask=3,time_epoch=145,
                                       expected_profile=[2000000,250000]))
    def window_observe(self, value):
        if hasattr(self,'model'):
            if not getattr(self,'allow_acquire',False):raise RuntimeError('NATIVE_TIME_OR_RUNTIME_RATE_NOT_QUALIFIED')
            self.model['observations']+=1
        return value
    mapping=dict(groups=[dict(channels=[1,2],tx_shared=[],editable=True)],
                 rx_center_frequency_hz={'1':2400000000,'2':2400000000})
    with patch.object(reuse.profiles,'STATE',directory/'profile-state.json'), \
            patch.object(reuse.profiles,'read_json',side_effect=read), \
            patch.object(reuse.profiles,'load_package',return_value={'id':'model'}), \
            patch.object(reuse,'atomic_save',side_effect=save), \
            patch.object(reuse,'allocate_epoch',return_value=148), \
            patch.object(reuse,'RuntimeProfile',Profile), \
            patch.object(reuse.rx,'read_applied',return_value={}), \
            patch.object(reuse.tx,'read_applied',return_value={}), \
            patch.object(reuse.radio,'modules',return_value=(SimpleNamespace(CATALOG=directory),
                                                           SimpleNamespace(stopped_tx=lambda hw:None),None)), \
            patch.object(reuse.radio,'describe',return_value=mapping), \
            patch.object(NativeTimeWindow,'__init__',window_init), \
            patch.object(NativeTimeWindow,'record',window_record), \
            patch.object(NativeTimeWindow,'observe',window_observe):
        wrapper=reuse.ReusableRxSession(session,directory,dict(profile_id='model',seconds=0))
        for op, fields in [('receiver_ready', dict(stream_id='41', configuration_sha256=prepared.record()['rx_sha256'])),
                           ('start',{}),('stop',{})]:
            wrapper.command(dict(op=op,token=wrapper.token,seq=wrapper.sequence+1,**fields))
        session.clock.now=1.
        result=wrapper.command(dict(op='prepare_epoch',token=wrapper.token,seq=wrapper.sequence+1,
                               frequency_hz=2400000000,previous_stream_id='41'))
        assert result['state']=='PREPARING_EPOCH' and not wrapper.closed
        assert result.get('epoch_info') is None
        while wrapper.preparing['stage']=='building':
            session.clock.now+=.01
            result=wrapper.command(dict(op='renew',token=wrapper.token,seq=wrapper.sequence+1))
        assert result['epoch_info']['native_time'] is None
        assert wrapper.epoch_info['template_changed_words']==2
        assert sum(offset in (*CONFIG_OFFSETS,*TX_OFFSETS) for offset,_ in board.writes)==2
        assert board.read(0x200)==0 and board.read(0x408)==148 and board.read(0x404)==1
        assert session.closed and session.state=='STOPPED'
        last_permission=wrapper.preparing['expires']
        wrapper.preparing['window'].allow_acquire=True
        session.clock.now+=.1
        wrapper.check()
        assert wrapper.current.expires==last_permission, 'qualification automatically renewed short lease'
        assert wrapper.state=='CONFIGURED' and wrapper.epoch_info['native_time']['observations']>=1
        assert wrapper.current is not session and wrapper.time_epoch==148
        assert not wrapper.receiver_ready, 'new object inherited old READY'
        wrapper.current.clock.now=last_permission+.001
        try:wrapper.check();raise AssertionError('short lease extended without a command')
        except TimeoutError:pass
        wrapper.close()


def audit_model(directory):
    """Actual board audit code tests chunk transfer/acknowledgement/drift rejection/active protection without hardware."""
    import base64,hashlib
    directory.mkdir();(directory/'request.json').write_text('{}')
    source=directory/'writes.jsonl';original=b'{"write":1}\n'*4000;source.write_bytes(original)
    session=object.__new__(reuse.ReusableRxSession)
    session.directory=directory;session.epoch=1;session.preparing=None
    board=Board(directory)
    session.current=SimpleNamespace(state='STOPPED',closed=False,board=board)
    descriptor=session._audit(dict(op='audit_open',token='model',seq=1))
    raw=b''
    for index in range((len(original)+12287)//12288):
        result=session._audit(dict(op='audit_chunk',token='model',seq=2,audit_id=descriptor['id'],index=index))
        raw+=base64.b64decode(result['data'])
    assert raw==original and source.read_bytes()==original
    assert hashlib.sha256(raw).hexdigest()==descriptor['sha256']
    for fields in (dict(audit_id='wrong',sha256=descriptor['sha256']),
                   dict(audit_id=descriptor['id'],sha256='0'*64)):
        try:session._audit(dict(op='audit_retire',token='model',seq=3,**fields));raise AssertionError('bad identity accepted')
        except RuntimeError:pass
        assert source.read_bytes()==original
    source.write_bytes(original+b'foreign')
    try:session._audit(dict(op='audit_retire',token='model',seq=3,audit_id=descriptor['id'],sha256=descriptor['sha256']));raise AssertionError('drift retired')
    except RuntimeError as error:assert 'ORIGINAL_CHANGED' in str(error)
    source.write_bytes(original)
    result=session._audit(dict(op='audit_retire',token='model',seq=4,audit_id=descriptor['id'],sha256=descriptor['sha256']))
    assert result['status']=='RETIRED_EXACT_VERIFIED' and source.read_bytes()==b''
    source.write_bytes(b'next\n');second=session._audit(dict(op='audit_open',token='model',seq=5))
    assert second['segments']==2 and second['previous_sha256']==descriptor['sha256']
    session.current.state='RUNNING'
    try:session._audit(dict(op='audit_open',token='model',seq=6));raise AssertionError('active audit accepted')
    except RuntimeError as error:assert 'REQUIRES_STOPPED' in str(error)
    assert source.read_bytes()==b'next\n'


def main():
    """Check old STOP restart rejection, new configuration/same time, zero-write foreign-word rejection and partial recovery."""
    observation = dict(observations=1, failed=False, mask=3, time_epoch=145,
                       expected_profile=[2000000, 250000])
    with TemporaryDirectory() as temporary, \
            patch.object(NativeTimeWindow, 'record', return_value=observation), \
            patch.object(NativeTimeWindow, 'observe', side_effect=lambda value:value), \
            patch.object(reuse.profiles, 'identity', return_value={'model': True}), \
            patch.object(reuse.profiles, 'private'), \
            patch('gr_usb_radio.emergency_stop'):
        directory = Path(temporary)
        session, board, prepared = fixture(directory)
        assert session.time_epoch == 145 and session.prepared.epoch == 147
        board.words[0x408] = 146
        try:session.check();raise AssertionError('foreign time epoch accepted')
        except RuntimeError as error:assert 'TIME_CONTROL_OR_EPOCH' in str(error)
        board.words[0x408] = 145
        for op, fields in [('receiver_ready', dict(stream_id='41', configuration_sha256=prepared.record()['rx_sha256'])),
                           ('start', {}), ('stop', {})]:
            reply = session.command(dict(op=op, token=session.token, seq=session.sequence+1, **fields))
        assert reply['state'] == 'STOPPED'
        try:
            session.command(dict(op='start', token=session.token, seq=session.sequence+1))
            raise AssertionError('old STOPPED object restarted')
        except RuntimeError as error:assert 'START_STATE' in str(error)
        preparation_interlock_model(directory/'preparation')
        audit_model(directory/'audit')
        original = dict(rx_hex=bytes(112).hex(), tx_hex=bytes(256).hex(), rx_epoch=144,
                        seed=0, checker=0, prepared=prepared.record())
        (directory/'request.json').write_text('{}')
        next_prepared = DigitalPreparation(3, 0, bytes([3])*112, bytes([4])*256, 148, 1234, 51, 52, 0)
        ledger = dict(schema=1, request_sha256=reuse._request_sha(directory), identity={'model': True},
                      current=reuse._entry(prepared), pending=reuse._entry(next_prepared))
        reuse.atomic_save(directory/'reuse-digital.json', ledger)
        board.words[0x200] = 0;board.words[0x404] = 0;board.words[0x204] = 148
        for offsets, first, second in ((CONFIG_OFFSETS, prepared.rx_native, next_prepared.rx_native),
                                       (TX_OFFSETS, prepared.tx_templates, next_prepared.tx_templates)):
            a = struct.unpack('<'+str(len(offsets))+'I', first)
            b = struct.unpack('<'+str(len(offsets))+'I', second)
            for n, offset in enumerate(offsets):board.words[offset] = a[n] if n%2 else b[n]
        word = CONFIG_OFFSETS[9];old = board.read(word);board.words[word] = 0xdeadbeef
        board.writes.clear()
        try:
            reuse.restore_configuration(board, original, prepared)
            raise AssertionError('foreign word restored')
        except RuntimeError as error:assert 'FOREIGN_WORD' in str(error)
        assert board.writes == [], 'wrote before validating full table'
        board.words[word] = old;board.words[0x200] = 0x80000003
        try:
            reuse.stop_configuration(board, original, prepared)
            raise AssertionError('partial active template accepted')
        except RuntimeError as error:assert 'ACTIVE_FOREIGN' in str(error)
        assert board.writes == []
        board.words[0x200] = 0
        assert reuse.stop_configuration(board, original, prepared) == []
        result = reuse.restore_configuration(board, original, prepared)
        assert result['rx_hex'] == original['rx_hex'] and result['tx_hex'] == original['tx_hex']
        assert result['rx_epoch'] == 144
        print(json.dumps(dict(status='PASS_MODEL_ONLY', separate_time_config=True,
            stopped_not_restarted=True, foreign_word_zero_write=True, partial_active_rejected=True,
            partial_restore_verified=True,time_disabled_before_template=True,new_session_waits_ready=True,qualification_does_not_renew=True,audit_chunk_restore=True,audit_drift_refused=True,audit_active_refused=True)))


if __name__ == '__main__':main()
# End of file: usb_reuse_model.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
