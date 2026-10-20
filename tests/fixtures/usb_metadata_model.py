"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_metadata_model.py
@brief Inject dual-ACK, partial-write and native-time faults into actually generated hot-metadata/recovery code.
@details MMIO/RF are explicit models; NativeTimeWindow and generated DigitalSession/owner are
         real implementations. Model success never establishes routing/CDC/board qualification; no devices/transmission.
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
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import gr_usb_reuse as reuse
from h102_runtime_profile import CONFIG_OFFSETS, TX_OFFSETS
from h102_runtime_rates import NativeTimeWindow
from h102_runtime_usb_session import DigitalSession, DigitalPreparation


class Board:
    """Advance four-phase state explicitly through actual restricted writes; inject readback/clock faults independently."""
    def __init__(self, directory):
        self.words = {0x204:147,0x404:1,0x408:145,0x420:0x19,0x424:3}
        self.writes=[];self.memory=self;self.phase=None;self.remaining=0
        self.hold_ack=False;self.hold_commit=False;self.fail_field=False;self.mid_drift=False;self.fail_after_start=False;self.profile_bad=False
        self.drift=False;self.tick=100;self.time_writes=[];self.source_busy=False
        self.hw=SimpleNamespace(directory=directory,phy=directory/'phy',settings=lambda:{},
            idle=lambda:None,verify=lambda **kw:None,calibration_idle=lambda:None)
    def validate(self):pass
    def idle(self):pass
    def sources_idle(self):return not self.source_busy
    def read(self, offset):
        if offset==0x42c and self.phase and not self.hold_ack:
            self.remaining-=1
            if self.remaining<=0:
                if self.phase=='begin':self.words[0x42c]=1;self.words[0x430]=0
                elif self.phase=='commit':self.words[0x42c]=0x74;self.words[0x204]=self.words[0x458]
                else:self.words[0x42c]=0x88
                self.phase=None
        return self.words.get(offset,0)
    def write(self, offset, value):
        if offset in CONFIG_OFFSETS and self.read(0x404):
            raise AssertionError('cold template write while native time enabled')
        self.words[offset]=value;self.writes.append((offset,value))
        if offset==0x200 and value and self.fail_after_start:self.profile_bad=True
    def write_time(self, offset, value):
        self.words[offset]=value;self.time_writes.append((offset,value))
    def write_metadata(self, offset, value):return reuse.write_metadata(self,offset,value)
    def mmio(self, offset, value):
        self.writes.append((offset,value))
        if offset==0x428:
            if value in (1,2,4):
                self.phase={1:'begin',2:'commit',4:'cancel'}[value]
                if value==2 and self.hold_commit:self.hold_ack=True
                self.remaining=4;self.words[0x42c]={1:2,2:0x42,4:0xca}[value]
                if value==4:self.words[0x40c]=1
            else:
                assert not self.read(0x404) and not self.read(0x42c)&0x73
                self.words[0x42c]=0;self.words[0x40c]=0;self.words[0x430]=0
        else:
            index=(offset-0x4c0)//4
            # Verify recovery responsibility persists before the first hot-template write.
            ledger=reuse._load(self.hw.directory)
            assert ledger['pending'] and ledger['metadata_transaction']['phase']=='intent'
            for target in reuse.METADATA_WORDS[index]:self.words[target]=value
            self.words[0x430]=self.read(0x430)|(1<<index)
            if self.fail_field and index==4:raise RuntimeError('MODEL_PARTIAL_WRITE_FAULT')
            if self.mid_drift and index==4:self.drift=True
    def snapshot(self, mask=0, epoch=None):
        self.tick+=100
        # The real Board.snapshot epoch argument belongs to the transport
        # owner (0x204/0x21c); native time comes independently from 0x408.
        actual=self.read(0x408)
        if mask and (self.read(0x200)!=(0x80000000|mask) or self.read(0x204)!=epoch):
            raise RuntimeError('TRANSPORT_OWNER_CHANGED')
        words=[]
        for _ in range(2):
            raw=(self.tick | actual<<208 | 1<<241 | 4_000_000<<242 |
                 2_000_000<<274 | int(self.drift)<<306 | 1<<338)
            words.extend((raw>>(32*n))&0xffffffff for n in range(12))
        result=dict(words=words,time_epoch=actual,source=1,monotonic_ns=self.tick*1000,mask=mask)
        if mask:result['transport']=dict(epoch=epoch,words=[0]*20)
        return result


class Cell:
    """Substitute physical mapping pointers while executing all actual write_metadata admission/readback checks."""
    def __init__(self,board,offset):self.board=board;self.offset=offset
    @property
    def value(self):return self.board.read(self.offset)
    @value.setter
    def value(self,value):self.board.mmio(self.offset,value)


def run_case(directory, *, fail=None):
    """Run actual generated ownership; recover normal commits independently and verify precise fault rejection."""
    directory.mkdir();board=Board(directory)
    for name,value in (('request.json',{}),('state.json',{'gnss_allocated_epoch':145}),
                       ('profile-state.json',{'configuration_id':1}),('allocation.json',{'last_epoch':147})):
        reuse.atomic_save(directory/name,value)
    words=[2_000_000,0,1,250_000,2_400_000_000,0,147,20,0,1234,0,41,0,1]*2
    native=struct.pack('<28I',*words);templates=bytes([2])*256
    prepared=DigitalPreparation(3,0,native,templates,147,1234,41,42,0)
    for offsets,raw in ((CONFIG_OFFSETS,native),(TX_OFFSETS,templates)):
        board.words.update(zip(offsets,struct.unpack('<'+str(len(offsets))+'I',raw)))
    board.words[0x408]=144
    baseline=board.snapshot()
    board.words[0x408]=145
    window=NativeTimeWindow(baseline,3,(2_000_000,250_000),145)
    window.observe(board.snapshot())
    profile=SimpleNamespace(expected_profile=(2_000_000,250_000),_hw=board.hw,check=lambda:None)
    session=DigitalSession(profile,board,prepared,0,window)
    class Profile:
        expected_profile=(2_000_000,250_000)
        def __init__(self,hw,board,*args):
            self._hw=hw;self.previous_rx_epoch=board.read(0x204);self.previous_time_epoch=board.read(0x408)
        def check(self):pass
        def templates(self,stream,tx_stream,epoch,device):
            values=list(struct.unpack('<28I',native))
            for start in (0,14):
                values[start+6]=epoch;values[start+11]=stream&0xffffffff;values[start+12]=stream>>32
            return struct.pack('<28I',*values),bytes([3])*256
    # The real initial RuntimeProfile already owns immutable template inputs.
    # The model's original profile must expose that same API for same-carrier reuse.
    profile.templates=Profile(board.hw,board).templates
    profile.previous_rx_epoch=147;profile.previous_time_epoch=145
    mapping=dict(groups=[dict(channels=[1,2],tx_shared=[],editable=True)],initial_calibrations='off',
        rx_center_frequency_hz={'1':2_400_000_000,'2':2_400_000_000})
    original_read=reuse.profiles.read_json;original_save=reuse.atomic_save
    allocation=Path('/var/lib/h102-rx/time-epoch.json')
    def redirect(path):return directory/'allocation.json' if Path(path)==allocation else path
    with patch.object(reuse.profiles,'STATE',directory/'profile-state.json'), \
         patch.object(reuse.profiles,'read_json',side_effect=lambda p:original_read(redirect(p))), \
         patch.object(reuse,'atomic_save',side_effect=lambda p,*a,**kw:original_save(redirect(p),*a,**kw)), \
         patch.object(reuse.profiles,'load_package',return_value={}), \
         patch.object(reuse,'allocate_epoch',return_value=148),patch.object(reuse,'RuntimeProfile',Profile), \
         patch.object(reuse.rx,'read_applied',return_value={}),patch.object(reuse.tx,'read_applied',return_value={}), \
         patch.object(reuse.radio,'modules',return_value=(SimpleNamespace(CATALOG=directory),
             SimpleNamespace(stopped_tx=lambda hw:None),None)),patch.object(reuse.radio,'describe',return_value=mapping):
        owner=reuse.ReusableRxSession(session,directory,dict(profile_id='model',seconds=0))
        for op,fields in [('receiver_ready',dict(stream_id='41',configuration_sha256=prepared.record()['rx_sha256'])),
                          ('start',{}),('stop',{})]:
            owner.command(dict(op=op,token=owner.token,seq=owner.sequence+1,**fields))
        if fail=='cadence':board.drift=True
        if fail=='late_ack':board.hold_ack=True
        if fail=='commit_ack':board.hold_commit=True
        if fail=='partial':board.fail_field=True
        if fail=='mid_cadence':board.mid_drift=True
        if fail=='source_busy':board.source_busy=True
        if fail=='g0_busy':board.words[0x30c]=1
        try:
            owner.command(dict(op='prepare_epoch',token=owner.token,seq=owner.sequence+1,
                frequency_hz=2_400_000_000,previous_stream_id='41'))
            limit=0
            while owner.preparing:
                if fail in ('late_ack','commit_ack'):time.sleep(.001)
                owner.command(dict(op='renew',token=owner.token,seq=owner.sequence+1));limit+=1
                assert limit<2000
        except (RuntimeError,TimeoutError):
            assert fail, 'healthy hot commit unexpectedly failed'
            assert board.read(0x204)==147 and not board.read(0x200)
            if fail in ('cadence','late_ack','source_busy','g0_busy'):
                assert not any(o>=0x4c0 for o,_ in board.writes)
                return
            # Stop native clock during WAIT; cancel cannot release frozen data until actual late ACK permits recovery.
            original=dict(rx_hex=bytes(112).hex(),tx_hex=bytes(256).hex(),rx_epoch=144,
                seed=0,checker=0,prepared=prepared.record())
            if fail=='commit_ack':
                before=list(board.time_writes)
                try:reuse.stop_configuration(board,original,prepared);raise AssertionError('missing ACK cancelled')
                except TimeoutError:pass
                assert board.time_writes==before, 'time disabled before native ACK drained'
                board.hold_ack=False
            reuse.stop_configuration(board,original,prepared)
            restored=reuse.restore_configuration(board,original,prepared)
            assert restored['rx_epoch']==144 and not board.read(0x42c)
            return
        assert fail in (None,'ready_binding','end_guard')
        assert owner.state=='CONFIGURED' and not owner.receiver_ready
        assert owner.current is not session and session.closed
        assert owner.current.profile is profile, 'same-carrier static profile was recreated'
        assert owner.current.time_window is window and board.read(0x408)==145
        assert not board.time_writes and board.read(0x204)==148
        assert reuse._image(board,TX_OFFSETS)==templates
        assert not any(o in (*CONFIG_OFFSETS,*TX_OFFSETS,0x204) for o,_ in board.writes)
        assert owner.epoch_info['metadata_commit']['status']==0x74
        assert window.record()['observations']>owner.epoch_info['metadata_commit']['previous_observations']
        # Exercise RUN and subsequent ownership queries after a real split of
        # configuration generation and native time epoch; preparation alone
        # never reaches the transport snapshot branch that failed on hardware.
        current=owner.prepared
        command=dict(op='receiver_ready_start',token=owner.token,seq=owner.sequence+1,
                     stream_id=str(current.rx_stream_id),configuration_sha256=current.record()['rx_sha256'])
        if fail=='ready_binding':
            command['configuration_sha256']='0'*64
            try:owner.command(command);raise AssertionError('bad READY started data')
            except RuntimeError as error:assert 'READY_BINDING' in str(error)
            assert not board.read(0x200) and owner.closed
            return
        if fail=='end_guard':
            board.fail_after_start=True
            def actual_profile_check():
                if board.profile_bad:raise RuntimeError('MODEL_PROFILE_CHANGED_AFTER_RUN')
            profile.check=actual_profile_check
            try:owner.command(command);raise AssertionError('failed final guard returned success')
            except RuntimeError as error:assert 'PROFILE_CHANGED_AFTER_RUN' in str(error)
            assert not board.read(0x200) and owner.closed
            return
        owner.command(command)
        for op in ('renew','snapshot','stop'):
            reply=owner.command(dict(op=op,token=owner.token,seq=owner.sequence+1))
            assert reply['native']['time_epoch']==145
            assert reply['native']['transport']['epoch']==148
            assert 'epoch_info' not in reply, 'static proof repeated outside preparation boundary'
        owner.close()
        original=dict(rx_hex=bytes(112).hex(),tx_hex=bytes(256).hex(),rx_epoch=144,
            seed=0,checker=0,prepared=prepared.record())
        if fail is None:
            saved=board.words[CONFIG_OFFSETS[0]];board.words[CONFIG_OFFSETS[0]]=0xdeadbeef
            before=list(board.writes)
            try:reuse.stop_configuration(board,original,prepared);raise AssertionError('foreign accepted')
            except RuntimeError as error:assert 'FOREIGN_WORD' in str(error)
            assert board.writes==before
            board.words[CONFIG_OFFSETS[0]]=saved
        reuse.stop_configuration(board,original,prepared)
        assert not board.read(0x404) and not board.read(0x40c) and not board.read(0x42c)
        restored=reuse.restore_configuration(board,original,prepared)
        assert restored['rx_epoch']==144 and restored['rx_hex']==original['rx_hex']


def main():
    """Run normal/time-drift/late-ACK cases once each; label evidence as model-only."""
    with TemporaryDirectory() as temporary,patch.object(reuse.profiles,'private'), \
         patch.object(reuse.profiles,'identity',return_value={'model':True}), \
         patch.object(reuse.ctypes.c_uint32,'from_buffer',side_effect=Cell), \
         patch('gr_usb_radio.emergency_stop'):
        root=Path(temporary)
        run_case(root/'healthy')
        run_case(root/'cadence',fail='cadence')
        run_case(root/'late-ack',fail='late_ack')
        run_case(root/'partial',fail='partial')
        run_case(root/'mid-cadence',fail='mid_cadence')
        run_case(root/'commit-ack',fail='commit_ack')
        run_case(root/'source-busy',fail='source_busy')
        run_case(root/'g0-busy',fail='g0_busy')
        run_case(root/'bad-ready',fail='ready_binding')
        run_case(root/'end-guard',fail='end_guard')
        board=Board(root);board.words[0x42c]=0x42
        try:reuse.quiesce_metadata(board,root);raise AssertionError('unknown transaction accepted')
        except (RuntimeError,FileNotFoundError):pass
        assert board.writes==[]
    print(json.dumps(dict(status='PASS_MODEL_ONLY',healthy_retained_epoch=True,
        actual_native_window=True,late_ack_refused=True,cadence_latched=True,
        foreign_word_zero_write=True,TX_frozen=True,post_drained=True,
        partial_write_recovered=True,clock_stopped_cancel_preserved=True)))


if __name__=='__main__':main()
# End of file: usb_metadata_model.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
