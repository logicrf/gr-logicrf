"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_rx_reuse_managed.py
@brief Retain board ownership/GNSS across RX generations and prepare new receivers after actual host completion.
@details Verify host main/post after each STOP/ACK/EOS before new templates/helpers.
         Never revive old FT processes/STOPPED objects; RF TX stays off; verify final board recovery/archive.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import hashlib
import json
import math
import time
import uuid
from threading import Thread

from .usb_radio_rx_managed import UsbRadioRxController


class UsbReuseRxController(UsbRadioRxController):
    """One lifecycle thread manages generations; monitors renew board leases while host state/generation changes serialize.

    Python handles control/evidence, never IQ. Paused board ownership retains three locks; other applications wait for
    final release. Recording releases ownership; warm tuning gaps never become apparently seamless IQ files.
    """
    def __init__(self, *args, fm=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fm = fm
        self.frequency_range = (88_000_000,108_000_000) if fm else (50_000_000,5_800_000_000)
        self.connection = None
        self.reuse_board = True
        self.defer_finish = True
        self.hold_for_tune = False
        self.paused = False
        self.epoch_count = 0
        self.host_finisher = None
        self.host_finish_error = None
        self.host_post_pending = False

    def prepare_direction(self, direction, mask):
        """Cold sessions never reuse prior host proofs; warm generations use newly frozen retune options."""
        with self.lifecycle:
            if self.prepared_options is None:
                self.host_finisher = None
                self.host_finish_error = None
                self.host_post_pending = False
                self.retained_host_post_verified = False
                self.epoch_count = 0
                self.paused = False
            options=super().prepare_direction(direction,mask)
            # First/warm generations share fields; managed profile IDs differ from native wire configuration/time epochs.
            metadata=json.loads(options.metadata_json)
            metadata.update(usb_configuration_id=self.expected['configuration_id'],time_epoch=options.usb_time_epoch)
            options.metadata_json=json.dumps(metadata,separators=(',',':'))
            return options

    def _stop_pair(self):
        """New guards combine control round trips; legacy guards retain full real stop handshakes."""
        if not (self.host_launch or {}).get('capabilities',{}).get('rx_stop_ack_v1'):
            return super()._stop_pair()
        with self.stop_lock:
            if self.board_stopped:return
            before=time.monotonic()
            proof=self._command('stop');self.board_stopped=True;self.guard_records['stop']=proof
            board_stopped=time.monotonic()
            result=self._host('send',dict(op='stop_ack',proof=proof))
            if (result.get('sent')!='stop_ack' or result.get('board_stop_verified') is not True or
                    result.get('helper_stop_observed') is not True):
                raise RuntimeError('USB_REUSE_HOST_STOP_HANDSHAKE_NOT_VERIFIED')
            self.guard_records['host_stop_ack']=result
            self.guard_records['stop_timing_seconds']=dict(board_stop=board_stopped-before,
                host_handshake=time.monotonic()-board_stopped)
            self._save('guards.json',self.guard_records)

    def _monitor(self):
        """Renewal still requires actual board checks; never read half-updated host fields during generation changes."""
        while not self.done.wait(.4):
            try:
                self._command('renew')
                with self.lifecycle:
                    if self.host_launch is not None:
                        status = self._host('status');self.host_status = status
                        state = status['state.json'] or {}
                        if state.get('error'):
                            raise RuntimeError('USB_HOST_DATA_ERROR:'+status['stdout'][-1000:])
                        if state.get('stop_requested') and not self.board_stopped:
                            self._stop_pair()
                        if state.get('running') is False and not status['main.json']:
                            raise RuntimeError('USB_HOST_MAIN_NOT_PASS')
                    if not self.pause_renewal:self.renew_local()
            except Exception as error:
                self.failure = repr(error);self.invalidate_local('USB_OWNER:'+str(error));return

    def _finish_host_receipt(self, epoch):
        """Verify host main/post and persistent archives without changing stopped board configuration."""
        phase_started=time.monotonic()
        phases={}
        if self.host_launch is None:
            raise RuntimeError('USB_REUSE_NO_HOST_EPOCH')
        deadline = time.monotonic()+12
        while True:
            status = self._host('status')
            if status['main.json']:
                break
            if (status['state.json'] or {}).get('running') is False and status['post.json']:
                self._save('host-final-%08d.json' % epoch, status)
                raise RuntimeError('USB_REUSE_HOST_MAIN_FAILED')
            if time.monotonic() >= deadline:
                raise TimeoutError('USB_REUSE_HOST_MAIN_DEADLINE')
            time.sleep(.02)
        phases['main_wait']=time.monotonic()-phase_started
        step_started=time.monotonic()
        final = self._host('finish', timeout=20)
        self._save('host-final-%08d.json' % epoch, final)
        if not final['main.json'].get('passed') or not final['post.json'].get('passed'):
            raise RuntimeError('USB_REUSE_HOST_MAIN_POST_NOT_PASSED')
        phases['finish_post']=time.monotonic()-step_started
        step_started=time.monotonic()
        with self.lifecycle:
            archive_supported=self.host_launch.get('capabilities',{}).get('epoch_archive_v1')
            if archive_supported:
                from .epoch_evidence import retire_host
                retire_host(self,epoch)
            else:
                self._save('epoch-retirement-not-supported.json',dict(host_epoch_archive_v1=False))
            self.retained_host_post_verified = True
            self.host_launch = None
            self.host_status = None
        phases['archive_and_retire']=time.monotonic()-step_started
        phases['total']=time.monotonic()-phase_started
        self._save('host-phases-%08d.json' % epoch,phases)

    def _finish_host_epoch(self, epoch):
        """Transfer both stopped endpoints' evidence concurrently; next preparation waits for every successful join.

        Board audit touches the same STOPPED owner's logs only; host cleanup touches the old FT process only.
        One failure still waits for peer cleanup and retains both errors; partial success never means completed recovery.
        """
        supported=(self.host_launch or {}).get('capabilities',{}).get('epoch_archive_v1')
        if not supported:
            return self._finish_host_receipt(epoch)
        started=time.monotonic();errors=[];audit_seconds=[]
        def audit():
            before=time.monotonic()
            try:
                from .epoch_evidence import checkpoint_audit
                checkpoint_audit(self,epoch)
            except Exception as error:
                errors.append('board_audit:'+repr(error))
            finally:audit_seconds.append(time.monotonic()-before)
        worker=Thread(target=audit,name='h102-usb-stopped-audit',daemon=False)
        worker.start()
        try:
            self._finish_host_receipt(epoch)
        except Exception as error:
            errors.append('host_post:'+repr(error))
        finally:
            worker.join(20)
        if worker.is_alive():errors.append('board_audit:join deadline')
        self._save('post-barrier-%08d.json' % epoch,dict(
            total_seconds=time.monotonic()-started,audit_seconds=audit_seconds,
            audit_joined=not worker.is_alive(),passed=not errors,errors=errors))
        if errors:raise RuntimeError('USB_EPOCH_POST_BARRIER:'+repr(errors))

    def guard(self, channel, stage):
        """Measure actual host read-posting-to-START through unchanged handshakes/protection/polling budgets."""
        started=time.monotonic()
        if stage=='usb-start' and (self.ready or {}).get('rx_reuse_ready_start_v1') is True:
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                status=self._host('status');self.host_status=status
                if (status['state.json'] or {}).get('ready'):break
                if self.failure:raise RuntimeError(self.failure)
                time.sleep(.03)
            else:raise TimeoutError('USB_REAL_HELPER_READY_TIMEOUT')
            result=self._command('receiver_ready_start',stream_id=self.expected['rx_stream_id'],
                                 configuration_sha256=self.expected['rx_sha256'])
            self.started=True;self.guard_records['start']=result
            self._save('guards.json',self.guard_records)
        else:result=super().guard(channel,stage)
        if stage=='usb-start':
            self._save('start-phases-%08d.json' % self.epoch_count,
                       dict(helper_ready_and_board_start=time.monotonic()-started))
        return result

    def finish_direction(self, direction):
        """Clean host asynchronously after healthy data EOS; next generation/final release await actual post."""
        if not self.hold_for_tune:
            return super().finish_direction(direction)
        with self.lifecycle:
            if (direction != 'rx' or self.native_ref().error() or not self.board_stopped or
                    not self.guard_records.get('stop', {}).get('ok')):
                raise RuntimeError('USB_REUSE_EPOCH_NOT_STOPPED')
            epoch = self.epoch_count
            self.host_post_pending = True
            self.host_finish_error = None
            self._save('epoch-%08d.json' % epoch, dict(guards=self.guard_records,
                prepared=self.expected, time_epoch=self.prepared_options.usb_time_epoch,
                host_post_required=True, host_final_receipt='host-final-%08d.json' % epoch))
            def finish_host():
                """End current host session only; keep board configuration; join before next generation."""
                try:
                    self._finish_host_epoch(epoch)
                except Exception as error:
                    self.host_finish_error = str(error)
                    self.failure = repr(error)
                    self.invalidate_local('USB_HOST_POST:'+str(error))
                finally:
                    self.host_post_pending = False
            self.host_finisher = Thread(target=finish_host, name='h102-usb-epoch-post', daemon=False)
            self.host_finisher.start()
            self.epoch_count += 1
            self.paused = True

    def _join_host_post(self):
        """Only the control thread waits for actual host post; data EOS and Qt do not block on cleanup."""
        if self.host_finisher is not None:
            self.host_finisher.join(20)
            if self.host_finisher.is_alive():raise TimeoutError('USB_REUSE_HOST_POST_NOT_JOINED')
        if self.host_finish_error:raise RuntimeError('USB_REUSE_HOST_POST:'+self.host_finish_error)

    def finish(self):
        """Continue independent board recovery despite host cleanup errors, then propagate the original error."""
        error = None
        try:self._join_host_post()
        except Exception as failure:error=failure
        try:super().finish()
        except Exception as recovery:
            if error is not None:
                raise RuntimeError('USB_HOST_POST_AND_RECOVERY:'+repr(error)+';'+repr(recovery)) from recovery
            raise
        if error is not None:raise error

    def retune(self, frequency):
        """Publish/bind new configuration/stream/actual time epoch only after old graph drains and host post succeeds."""
        if (isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or
                not math.isfinite(frequency) or frequency != int(frequency) or
                not 50_000_000 <= frequency <= 5_800_000_000):
            raise ValueError('USB_REUSE_FREQUENCY')
        phase_started=time.monotonic()
        self._join_host_post()
        phases=dict(previous_post_wait=time.monotonic()-phase_started)
        with self.lifecycle:
            if not self.paused or self.host_launch is not None or not self.board_stopped:
                raise RuntimeError('USB_REUSE_REQUIRES_PAUSED_HOST_POST')
            # prepare_epoch native transport counts belong to the new generation, not accumulated old totals.
            self.started = False
            step_started=time.monotonic()
            reply = self._command('prepare_epoch', frequency_hz=int(frequency),
                                  previous_stream_id=self.expected['rx_stream_id'])
            deadline = time.monotonic()+8
            while reply.get('state') == 'PREPARING_EPOCH':
                if time.monotonic() >= deadline:
                    raise TimeoutError('USB_REUSE_NATIVE_TIME_ACQUISITION_TIMEOUT')
                time.sleep(.03)
                reply = self._command('renew')
            phases['board_prepare']=time.monotonic()-step_started
            step_started=time.monotonic()
            epoch_info = reply.get('epoch_info', {})
            prepared = epoch_info['prepared'];config = bytes.fromhex(epoch_info['configuration_hex'])
            time_proof = epoch_info.get('native_time') or {}

            previous = self.expected
            retained = epoch_info.get('time_retained_after_dual_native_metadata_commit') is True
            proof = epoch_info.get('metadata_commit') or {}
            if retained and (not self.ready or self.ready.get('rx_metadata_capability') != 3 or
                    proof.get('abi') != 1 or proof.get('capability') != 3 or proof.get('status') != 0x74 or
                    proof.get('dirty') != 0x1ff or proof.get('configuration_id') != prepared['configuration_id'] or
                    proof.get('time_epoch') != self.prepared_options.usb_time_epoch or
                    epoch_info['time_epoch'] != self.prepared_options.usb_time_epoch or
                    type(proof.get('previous_observations')) is not int or
                    type(proof.get('observations')) is not int or
                    proof['observations'] <= proof['previous_observations'] or
                    time_proof.get('observations') != proof['observations']):
                raise RuntimeError('USB_REUSE_METADATA_COMMIT_PROOF')
            if (reply.get('state') != 'CONFIGURED' or prepared['rx_mask'] != previous['rx_mask'] or
                    prepared['tx_mask'] or prepared['device_id'] != previous['device_id'] or
                    prepared['configuration_id'] <= previous['configuration_id'] or
                    prepared['rx_stream_id'] == previous['rx_stream_id'] or len(config) != 112 or
                    hashlib.sha256(config).hexdigest() != prepared['rx_sha256'] or
                    (not retained and epoch_info['time_epoch'] <= self.prepared_options.usb_time_epoch) or
                    time_proof.get('time_epoch') != epoch_info['time_epoch'] or
                    time_proof.get('failed') is not False or time_proof.get('observations', 0)<1 or
                    not epoch_info.get('previous_stopped_not_restarted')):
                raise RuntimeError('USB_REUSE_EPOCH_READBACK')
            mapping = epoch_info['radio']
            if any(mapping['rx_center_frequency_hz'][str(ch)] != frequency for ch in self.rx_channels):
                raise RuntimeError('USB_REUSE_FREQUENCY_READBACK')
            native = self.native_ref();native.set_center_frequency(int(frequency))
            self.info['radio'] = mapping
            options = native.options();options.center_frequency = frequency
            path = self.run_output/('configuration-%08d.bin' % self.epoch_count)
            path.write_bytes(config)
            options.usb_configuration = str(path)
            options.usb_stream_id = int(prepared['rx_stream_id'])
            options.usb_time_epoch = epoch_info['time_epoch']
            options.metadata_json = json.dumps(dict(self.info, stream_source='native_usb_rx', RF_TX=False,
                usb_configuration_id=prepared['configuration_id'], time_epoch=epoch_info['time_epoch']), separators=(',', ':'))
            options.usb_socket = str(self.project/'envs/gr-usb'/(uuid.uuid4().hex+'.sock'))
            self.expected = prepared
            self.host_session = 'h102-gr-usb-'+uuid.uuid4().hex
            self.board_stopped = False
            self.guard_records = {}
            self.transport_previous = {}
            self.transport_observations = 0
            self.prepared_options = options
            self.renew_local()
            self.retained_host_post_verified = False
            phases['bind_new_epoch']=time.monotonic()-step_started
            step_started=time.monotonic()
            self.host_launch = self._host('launch', self.host_request(options,
                sum(1 << (ch-1) for ch in self.rx_channels), False))
            self.paused = False
            self.hold_for_tune = False
            self._save('prepared-%08d.json' % self.epoch_count, epoch_info)
            phases['host_launch']=time.monotonic()-step_started
            phases['total']=time.monotonic()-phase_started
            self._save('retune-phases-%08d.json' % self.epoch_count,phases)
            return reply

# End of file: usb_rx_reuse_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
