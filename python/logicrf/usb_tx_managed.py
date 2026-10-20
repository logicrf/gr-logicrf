"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_tx_managed.py
@brief Coordinate native digital TX ownership with an unprivileged FT601 transmitter process.
@details Independent monitor renews permits and advances DRAIN/prefill/PLAY; IQ never enters Python.
         Finite END checks actual SSI consumed counts/signatures before STOP/ACK; both independent posts remain native.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.5.0
@date 2026-10-16
@note RF/DDS/DMA stay off; the initial entry supports explicit RAMP32 digital tests only.
"""
import json
from threading import Event, RLock
import time
from .usb_managed import UsbController


class UsbTxController(UsbController):
    """Independent TX handshake with stop-lock before stage-lock; monitor never waits for stop-lock while holding stage-lock."""
    def prepare(self, mask):
        """Check finite lengths/digital ranges before reusing pinned board ownership and managed host launch."""
        options=self.native_ref().options()
        if (options.tx_validation!='ramp32' or options.tx_samples%16384 or
                options.tx_samples<16384 or options.tx_samples>options.sample_rate*(self.seconds+1)):
            raise ValueError('USB_TX_DIGITAL_LENGTH_OR_PATTERN')
        self.phase_lock=RLock();self.phase='initial';self.digital_end=Event()
        self.tx_observations=0;self.tx_snapshot=None;self.stop_expected=False
        return super().prepare(mask)

    def _command(self, op, **values):
        """Retain original board TX snapshot; base class preserves strict sequence and sole SSH reader."""
        reply=super()._command(op,**values)
        if reply.get('tx') and reply['tx'].get('snapshot'):
            self.tx_snapshot=reply['tx']['snapshot']['decoded'];self.tx_observations+=1
            path=self.run_output/'tx-snapshots.jsonl'
            if path.exists() and path.stat().st_size>4*1024*1024:path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a') as stream:
                stream.write(json.dumps(dict(op=op,seq=reply['seq'],tx=reply['tx']),separators=(',',':'))+'\n')
        return reply

    def _check_data(self, snapshot, final=False):
        """Compare actual SSI consumption with Sink input signatures; USB submission cannot replace this check."""
        options=self.native_ref().options();mask=self.expected['tx_mask']
        if snapshot['usb_flags']&31:raise RuntimeError('USB_TX_NATIVE_PROTOCOL_FAULT')
        complete=True
        for i,channel in enumerate(snapshot['channels']):
            if channel['missing'] or channel['pattern_errors'] or channel['cadence_errors'] or channel['flags']&8:
                raise RuntimeError('USB_TX_SSI_UNDERFLOW_PATTERN_OR_CADENCE')
            if not mask&(1<<i):
                if any(channel[k] for k in ('consumed','observed','missing','pattern_errors','signature')) or snapshot['frames'][i]:
                    raise RuntimeError('USB_TX_UNSELECTED_CHANNEL_ACTIVITY')
                continue
            target=self.native_ref().tx_stats(i+1)['target_samples']
            if target and channel['consumed']>target:raise RuntimeError('USB_TX_SSI_CONSUMED_TOO_MANY')
            done=bool(channel['flags']&4) and target and channel['consumed']==target
            complete=complete and done
            if final:
                stats=self.native_ref().tx_stats(i+1)
                if (not done or channel['observed']!=target or
                        stats['socket_sent_samples']!=target or
                        channel['signature']!=stats['signature'] or
                        snapshot['frames'][i]!=target//2048):
                    raise RuntimeError('USB_TX_END_TO_END_SSI_SIGNATURE_OR_COUNT')
        return complete

    def _step(self, status, reply):
        """Advance observed actual stages only; never enter stop-lock while holding stage-lock."""
        state=status['state.json'] or {}
        if self.board_stopped or self.stop_expected:return
        # CONFIGURED/STOPPED renewals legitimately return snapshot=None; this is not a malformed reply.
        snapshot=((reply.get('tx') or {}).get('snapshot') or {}).get('decoded')
        if state.get('error'):raise RuntimeError('USB_TX_HOST_ERROR:'+status['stdout'][-1000:])
        if self.phase=='initial' and state.get('drain_requested'):
            self._command('drain');self.phase='draining'
        elif self.phase=='draining' and snapshot and snapshot['usb_flags']&0x480==0x480:
            self._host('send',dict(op='drain_idle',proof=reply));self.phase='abort'
        elif self.phase=='abort' and state.get('flushed'):
            proof=self._command('finish_drain')
            self._host('send',dict(op='drain_finished',proof=proof));self.phase='queue'
        elif self.phase=='queue' and state.get('ready'):
            self._command('sender_ready',stream_id=self.expected['tx_stream_id'],configuration_sha256=self.expected['tx_sha256'])
            self._command('arm');self.phase='armed'
        elif self.phase=='armed' and snapshot:
            if snapshot['usb_flags']&31:raise RuntimeError('USB_TX_PREFILL_FAULT')
            if all(snapshot['frames'][i]>=2 for i in range(2) if self.expected['tx_mask']&(1<<i)):
                proof=self._command('start');self._host('send',dict(op='play',proof=proof))
                self.native_ref().confirm_tx_playing()
                self.started=True;self.phase='playing';self.guard_records['start']=proof
        elif self.phase=='playing' and snapshot:
            complete=self._check_data(snapshot)
            if state.get('stop_requested') and not state.get('usb_done'):
                raise RuntimeError('USB_TX_HOST_STOPPED_BEFORE_FINITE_END')
            if complete and state.get('usb_done'):self.digital_end.set()

    def _monitor(self):
        """Renewal/host handshake are independent of input; failure revokes C++ permission without automatically replaying PLAY."""
        while not self.done.wait(.1):
            native=self.native_ref()
            if native is None:return
            try:
                with self.phase_lock:
                    reply=self._command('renew')
                    if self.host_launch is not None:
                        status=self._host('status');self.host_status=status
                        self._step(status,reply)
                    if not self.pause_renewal:self.renew_local()
            except Exception as error:
                self.failure=repr(error);self.invalidate_local('USB_TX_OWNER:'+str(error));return

    def guard(self, channel, stage):
        """After full input, await actual END/recovery while continuing data supply during PLAY round trips."""
        if stage=='usb-tx-stop':self._stop_pair(cancel=True);return
        if stage=='usb-tx-play':
            deadline=time.monotonic()+12
            while not self.started:
                if self.failure:raise RuntimeError(self.failure)
                if time.monotonic()>deadline:raise TimeoutError('USB_TX_PLAY_DEADLINE')
                time.sleep(.01)
            return
        if stage!='usb-tx-finish':raise ValueError('USB_TX_GUARD_STAGE')
        native=self.native_ref();options=native.options();pending=0
        for ch in (1,2):
            if self.expected['tx_mask']&(1<<(ch-1)):
                consumed=(self.tx_snapshot or {}).get('channels',[{},{}])[ch-1].get('consumed',0)
                pending=max(pending,max(0,native.tx_stats(ch)['socket_sent_samples']-consumed))
        deadline=time.monotonic()+pending/options.sample_rate+20
        while not self.digital_end.wait(.03):
            if self.failure:raise RuntimeError(self.failure)
            if time.monotonic()>deadline:raise TimeoutError('USB_TX_SSI_END_DEADLINE')
        self._stop_pair(cancel=False)

    def _stop_pair(self, cancel=True):
        """Capture SSI/STOP, then continuous quiet DRAIN/ACK; after USB close, confirm quiet again and leave DRAIN."""
        with self.stop_lock:
            if self.board_stopped:return
            with self.phase_lock:
                self.stop_expected=True
                proof=self._command('stop');snapshot=proof['tx']['snapshot']['decoded']
                self.guard_records['stop']=proof
                # Validation failure still requires independent drain/ACK; never strand the device in blocking SDK cleanup.
                verification_error=None
                if not cancel:
                    try:self._check_data(snapshot,final=True)
                    except Exception as error:verification_error=error
                self.guard_records['final_ssi']=snapshot
                self._command('drain');deadline=time.monotonic()+7
                while True:
                    quiet=self._command('renew')
                    if quiet['tx']['snapshot']['decoded']['usb_flags']&0x480==0x480:break
                    if time.monotonic()>deadline:raise TimeoutError('USB_TX_STOP_DRAIN_IDLE')
                    time.sleep(.01)
                if self.host_launch is not None:
                    status=self._host('status');state=status['state.json'] or {}
                    if state.get('running'):
                        if not state.get('stop_requested'):self._host('send',dict(op='stop'))
                        while True:
                            status=self._host('status');state=status['state.json'] or {}
                            if state.get('stop_requested'):break
                            self._command('renew')
                            if time.monotonic()>deadline:raise TimeoutError('USB_TX_HOST_STOP_REQUEST')
                            time.sleep(.02)
                        self._host('send',dict(op='ack',proof=quiet))
                        while True:
                            status=self._host('status');state=status['state.json'] or {}
                            if state.get('running') is False:break
                            self._command('renew')
                            if time.monotonic()>deadline:raise TimeoutError('USB_TX_HOST_CLOSE_DEADLINE')
                            time.sleep(.03)
                quiet=self._command('renew')
                if quiet['tx']['snapshot']['decoded']['usb_flags']&0x480!=0x480:
                    raise RuntimeError('USB_TX_POST_CLOSE_DRAIN_NOT_QUIET')
                self._command('finish_drain');self.board_stopped=True;self.phase='stopped'
                self._save('guards.json',self.guard_records)
                if verification_error:raise verification_error

# End of file: usb_tx_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
