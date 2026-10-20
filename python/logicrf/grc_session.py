"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file grc_session.py
@brief Asynchronous RX start/stop/tuning and scoped quality events for editable GRC flowgraphs.
@details Control the caller's top_block only, without hidden DSP graphs or standalone SDR application imports.
         One worker owns hardware; another waits for the scheduler; all IQ still uses native blocks.
         Single/dual RX IIO can reuse ownership after ADC windows close; USB explicitly releases fully where required.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
from copy import deepcopy
import math
import os
from threading import Condition, Event, Thread
import time
import weakref


def make_grc_rx_device(*, backend='iio', host='DEVICE_IP', channels=(1,),
                       profile='1p536msps-0p5mhz', sample_rate=1536000,
                       frequency=98100000, gain_db=20, fm=False, samples=0, **kwargs):
    """Construct RX plans without connecting; connect every declared channel in GRC; RF TX stays off.

    fm requires single RX, 1.536 MSPS and 88-108 MHz, using the released paused-retune protocol.
    IIO uses the new full-band RX entry; legacy FM actor scope is unchanged.
    """
    from . import make_device, make_duplex_device
    if 'evidence' not in kwargs and os.environ.get('LOGICRF_GRC_EVIDENCE_ROOT'):
        kwargs['evidence'] = os.environ['LOGICRF_GRC_EVIDENCE_ROOT']
    channels = tuple(channels)
    if channels not in ((1,), (2,), (1, 2)):
        raise ValueError('GRC_RX_CHANNELS')
    if not math.isfinite(gain_db) or not -3 <= gain_db <= 30:
        raise ValueError('RX_GAIN_RANGE')
    if fm and (len(channels) != 1 or sample_rate != 1536000 or not 88e6 <= frequency <= 108e6):
        raise ValueError('FM_REQUIRES_SINGLE_RX_1536000_88_108MHZ')
    if type(samples) is not int or samples < 0 or (backend == 'usb' and samples):
        raise ValueError('FINITE_SAMPLES_IIO_OR_MOCK_ONLY')
    if backend == 'mock':
        board = make_device('mock', configuration=profile, sample_rate=sample_rate,
                            center_frequency=frequency, mock_samples=samples, **kwargs)
        return board
    board = make_duplex_device(backend=backend, uri='ip:' + host,
        rx_channels=channels, tx_channels=(), rf_enabled=False,
        configuration=profile, sample_rate=sample_rate, rx_frequency=frequency,
        tx_frequency=frequency, rx_gain_db={ch: gain_db for ch in channels},
        usb_seconds=0, sample_limit=samples, **kwargs)
    if backend == 'iio':
        from .fm_managed import FmDevice
        from .iio_rx_batch_managed import IioBatchRxController
        from .fm_scan_managed import FmScanController
        old = board.controller
        controller_type = FmScanController if fm else IioBatchRxController
        controller = controller_type(board.rx_native, old.host, old.username, old.known_hosts,
            old.runtime, old.lock_path, old.firmware_lock, old.evidence, old.profile,
            old.provider, old.machine_id, old.pattern, rx_native=board.rx_native,
            rx_channels=channels, tx_channels=(), rf_enabled=False,
            rx_gain_db={ch: gain_db for ch in channels}, stop_event=Event(), fm=fm)
        # Retain 32 kernel buffers/34-block drain; smaller blocks reduce stale-data delay between generations.
        if fm:
            board.rx_native.set_frame_samples(32768)
        else:
            # 32 buffers provide roughly 0.8-1.6 s low-rate margin; high rates remain capped at 262144 samples.
            frame = max(1024, min(262144, 1 << max(0, math.ceil(sample_rate/40)-1).bit_length()))
            board.rx_native.set_frame_samples(frame)
        board = FmDevice(None, board.rx_native, controller)
    elif backend == 'usb':
        from .iio_stream_managed import DuplexDevice
        from .usb_rx_reuse_managed import UsbReuseRxController
        old = board.controller
        controller = UsbReuseRxController(board.rx_native, old.host, old.username, old.known_hosts,
            old.runtime, old.lock_path, old.firmware_lock, old.evidence, old.profile,
            old.provider, old.machine_id, old.project, old.host_guard, old.host_provider, old.seconds,
            rx_native=board.rx_native, rx_channels=channels, tx_channels=(), rf_enabled=False,
            rx_gain_db={ch: gain_db for ch in channels}, fm=fm)
        board = DuplexDevice(None, board.rx_native, controller)
    return board


def observation(probe, backend, rate):
    """Merge bounded native snapshots, distinguishing relative sample time, device digital time and uncalibrated UTC.

    Without native tags never invent ADC indices. USB time_valid means valid transport time, not calibrated
    antenna-port UTC. Python reads bounded statistics/latest tags only, never IQ arrays.
    """
    import pmt
    stats = probe.snapshot()
    metadata = pmt.to_python(probe.metadata())
    frame = metadata.get('h102_rx_frame') if isinstance(metadata, dict) else None
    result = dict(samples=stats.get('samples', 0), relative_seconds=stats.get('samples', 0)/rate,
        signal=probe.signal_snapshot(), counters=stats, metadata=metadata,
        sample_index_kind='host_output_not_adc' if backend == 'iio' else
            'synthetic' if backend == 'mock' else 'native_adc_frame_index',
        time_quality='relative_only', utc_accuracy_verified=False)
    if frame:
        valid = bool(frame.get('time_valid') and frame.get('tick_hz') and frame.get('time_epoch'))
        result.update(time_quality='device_digital_valid_utc_uncalibrated' if valid else 'invalid',
                      hardware_time=deepcopy(frame))
    return result


class GrcRxSession:
    """Serialize an existing top_block through thread-safe requests without Qt ownership or DSP reconnection.

    notify must be nonblocking/thread-safe. Reusable IIO/USB pauses retain ownership; USB host post may finish
    after data EOS, so next generation/release waits for actual proof. Retain recovery failures.
    """
    def __init__(self, graph, board, *, probes=None, notify=None, on_epoch=None,
                 before_stop=None, ready_timeout=125, recording=None, health_check=None):
        self.graph, self.board = graph, board
        self.probes = probes or {}
        self.notify = notify or (lambda event: None)
        self.on_epoch = on_epoch or (lambda frequency, epoch: None)
        self.before_stop = before_stop or (lambda: None)
        self.ready_timeout = ready_timeout
        self.recording = recording
        self.health_check = health_check or (lambda: {})
        self.condition = Condition()
        self.worker = self.waiter = None
        self.state = 'idle'
        self.want_running = self.releasing = self.closing = False
        self.target = None
        self.epoch = 0
        self.events = []
        self.last_result = None
        self.failed = False
        self.notification_error = None
        self.backend = board.options().backend
        self.controller = getattr(board, 'controller', None)
        self.persistent = bool(self.controller and hasattr(self.controller, 'hold_for_tune'))
        if self.controller is not None:
            session_ref=weakref.ref(self)
            def startup_notify(row):
                session=session_ref()
                if session is not None:session._emit('startup_stage', **row)
            self.controller.startup_notify = startup_notify

    def _emit(self, event, **fields):
        value = dict(schema=1, event=event, monotonic=time.monotonic(), epoch=self.epoch, **fields)
        # Bound event history and avoid accumulating frequent statistics; external event files retain complete acceptance evidence.
        if event != 'stats':
            self.events.append(value)
            del self.events[:-256]
        try:
            self.notify(value)
        except Exception as error:
            self.notification_error = str(error)
            with self.condition:
                self.want_running = False
                self.releasing = True
                self.condition.notify_all()

    def reject(self, action, error):
        """Record declined requests without interrupting healthy capture, fabricating success or silently dropping errors."""
        self._emit('rejected', action=action, error=str(error))

    def _state(self, state, **fields):
        with self.condition:
            changed = self.state != state
            self.state = state
            self.condition.notify_all()
        if changed:
            self._emit('state', state=state, **fields)

    def start(self):
        """Accept start/resume intent immediately; async events confirm actual preparation and first IQ."""
        with self.condition:
            if self.failed or self.state in ('failed', 'closed') or self.closing or self.releasing:
                raise RuntimeError('SESSION_REQUIRES_NEW_FLOWGRAPH')
            if not self.probes:
                raise RuntimeError('GRC_READY_REQUIRES_IQ_OBSERVERS')
            self.want_running = True
            self._emit('accepted', action='start')
            if self.worker is None or not self.worker.is_alive() or self.state == 'released':
                previous = self.worker

                def launch():
                    if previous and previous.is_alive():
                        previous.join()
                    self._run()

                self.worker = Thread(target=launch, name='h102-grc-owner', daemon=False)
                self.worker.start()
            self.condition.notify_all()

    def pause(self):
        """Stop/drain data; reusable entries retain ownership, legacy USB releases fully; bounded preparation cancellation is supported."""
        with self.condition:
            self.want_running = False
            self.target = None
            if self.recording:
                self.releasing = True
            if self.state == 'preparing' and self.controller and hasattr(self.controller, 'stop_event'):
                self.controller.stop_event.set()
            self._emit('accepted', action='pause')
            self.condition.notify_all()

    def tune(self, frequency):
        """Coalesce latest frequency in 50 MHz-5.8 GHz; identical frequencies cause no hardware transaction.

        Retuning creates explicit data gaps with separate counts/guards per generation, not seamless recording.
        """
        if isinstance(frequency, bool) or not math.isfinite(frequency) or not 50e6 <= frequency <= 5.8e9:
            raise ValueError('FREQUENCY_RANGE')
        frequency = int(frequency)
        if self.persistent and not self.controller.frequency_range[0] <= frequency <= self.controller.frequency_range[1]:
            raise ValueError('FM_FREQUENCY_RANGE' if self.controller.fm else 'RX_FREQUENCY_RANGE')
        with self.condition:
            if self.failed or self.closing or self.releasing or self.state in ('failed', 'closed'):
                raise RuntimeError('SESSION_CLOSING_OR_FAILED')
            if self.recording and self.worker and self.worker.is_alive():
                raise RuntimeError('RECORDING_PARAMETERS_LOCKED')
            if frequency == self.board.options().center_frequency and self.target is None:
                return False
            self.target = dict(frequency=frequency, requested=time.monotonic())
            if not self.worker or not self.worker.is_alive():
                self.board.set_center_frequency(frequency)
                self.target = None
                self._emit('plan', frequency_hz=frequency, hardware_applied=False)
            self._emit('accepted', action='tune', frequency_hz=frequency)
            self.condition.notify_all()
        return True

    def release(self, *, close=False):
        """Release finally/asynchronously; clean hardware after preparation and never call close concurrently."""
        with self.condition:
            self.want_running = False
            self.target = None
            self.releasing = True
            self.closing |= close
            if self.controller and hasattr(self.controller, 'stop_event') and self.state == 'preparing':
                self.controller.stop_event.set()
            if not self.worker or not self.worker.is_alive():
                self._state('closed' if close else 'released', owner_held=False)
                self.releasing = False
            self.condition.notify_all()

    def join(self, timeout=None):
        """Non-Qt tests/process cleanup may wait for the control thread; report whether it actually ended."""
        if self.worker:
            self.worker.join(timeout)
        return self.worker is None or not self.worker.is_alive()

    def _wait_graph(self):
        self.graph.wait()

    def _drain(self, *, retain):
        """Keep GNU consuming through complete native blocks; pause reuse only after all guards close."""
        before=time.monotonic()
        self._state('stopping_data')
        self.before_stop()
        after_before_stop=time.monotonic()
        stop_requested=after_before_stop
        if self.persistent:
            self.controller.hold_for_tune = retain
        if self.waiter and self.waiter.is_alive():
            if hasattr(self.board, 'stop_rx'):
                self.board.stop_rx()
            else:
                self.board.request_rx_stop()
            stop_requested=time.monotonic()
            self.waiter.join(90)
            if self.waiter.is_alive():
                raise TimeoutError('GRC_GRAPH_NOT_DRAINED')
        graph_ended=time.monotonic()
        if self.board.error():
            raise RuntimeError(self.board.error())
        native = getattr(self.board, 'rx_native', getattr(self.board, 'native', self.board))
        counts = {}
        for channel, probe in self.probes.items():
            accepted = native.received(channel)
            output = native.queue_stats(channel).get('output_samples', 0)
            observed = probe.snapshot()['samples']
            counts[str(channel)] = dict(accepted=accepted, output=output, observed=observed)
            if accepted != output or output != observed:
                self._emit('drain_failed', channels=counts)
                raise RuntimeError('GRC_RX_NOT_FULLY_DRAINED:RX%d:%d/%d/%d' %
                                   (channel, accepted, output, observed))
        deferred = bool(self.backend == 'usb' and getattr(self.controller, 'defer_finish', False))
        self._emit('data_stopped', owner_held=bool(retain and self.persistent) or deferred,
                   recovery_pending=deferred, channels=counts,
                   host_post_pending=bool(getattr(self.controller,'host_post_pending',False)),
                   stage_seconds=dict(before_stop=after_before_stop-before,
                       request_stop=stop_requested-after_before_stop,
                       await_eos=graph_ended-stop_requested,
                       verify_counts=time.monotonic()-graph_ended))

    def _run(self):
        """Sole device-control worker; every error reaches independent recovery with separately retained failures."""
        from .control import device_status
        error = recovery_error = None
        try:
            if self.recording:
                self.recording.begin()
            while True:
                with self.condition:
                    if self.releasing or self.closing:
                        break
                    if not self.want_running:
                        self._state('paused' if self.persistent and getattr(self.controller, 'paused', False)
                                    else 'idle', owner_held=bool(self.persistent and getattr(self.controller, 'paused', False)))
                        self.condition.wait(.1)
                        if self.board.error():
                            raise RuntimeError(self.board.error())
                        continue
                    target, self.target = self.target, None
                requested = target['requested'] if target else time.monotonic()
                frequency = target['frequency'] if target else self.board.options().center_frequency
                if self.controller and hasattr(self.controller, 'stop_event'):
                    self.controller.stop_event.clear()
                if self.persistent and self.controller.paused:
                    self._state('tuning')
                    self.controller.retune(frequency)
                elif target:
                    self.board.set_center_frequency(frequency)
                self.on_epoch(frequency, self.epoch)
                self._state('preparing', frequency_hz=frequency)
                previous_generations = {ch: probe.snapshot().get('generation', 0) for ch, probe in self.probes.items()}
                self.graph.start()
                self.waiter = Thread(target=self._wait_graph, name='h102-grc-wait', daemon=True)
                self.waiter.start()
                deadline = time.monotonic() + self.ready_timeout
                # Beyond readiness, require actual IQ from this generation on observation branches; preparation replies are not data.
                def has_iq():
                    for channel, probe in self.probes.items():
                        snapshot = probe.snapshot()
                        if snapshot.get('generation', 0) <= previous_generations[channel] or not snapshot.get('samples'):
                            return False
                    return bool(self.probes)

                while not ((self.board.ready() and (not self.probes or has_iq())) or
                           (has_iq() and not self.waiter.is_alive() and
                            (self.board.options().sample_limit or self.board.options().mock_samples))):
                    if self.board.error():
                        raise RuntimeError(self.board.error())
                    self.health_check()
                    if not self.waiter.is_alive() or time.monotonic() > deadline:
                        raise TimeoutError('GRC_START_NO_VALID_IQ')
                    time.sleep(.01)
                self._state('running', frequency_hz=frequency, ready_seconds=time.monotonic()-requested,
                            actual_info=deepcopy(getattr(self.controller, 'info', None)))
                last_stats = 0
                audio_started = False
                while self.waiter.is_alive():
                    if self.board.error():
                        raise RuntimeError(self.board.error())
                    with self.condition:
                        if not self.want_running or self.target is not None or self.releasing or self.closing:
                            break
                    now = time.monotonic()
                    if now-last_stats > .2:
                        if self.recording:
                            self.recording.check()
                        health = self.health_check()
                        audio = health.get('audio', {})
                        if not audio_started and audio.get('submitted_samples') and audio.get('playback_started'):
                            self._emit('audio_started', audio_service_seconds=time.monotonic()-requested,
                                       observation_period_seconds=.2, acoustic_latency_verified=False)
                            audio_started = True
                        self._emit('stats', health=health, channels={str(ch): observation(probe, self.backend,
                            self.board.options().sample_rate) for ch, probe in self.probes.items()})
                        last_stats = now
                    time.sleep(.01)
                with self.condition:
                    retain = self.persistent and not self.recording and not self.releasing and not self.closing
                self._drain(retain=retain)
                self._emit('epoch_complete', device=device_status(self.board),
                    health=self.health_check(),
                    channels={str(ch): observation(p, self.backend, self.board.options().sample_rate)
                              for ch, p in self.probes.items()})
                self.epoch += 1
                if (self.backend == 'usb' and getattr(self.controller, 'defer_finish', False)
                        and not (self.persistent and self.controller.paused)):
                    # Native EOS is confirmed, but both endpoint posts must finish before another prepare.
                    # Consume this generation's stop intent before slow post; retain new start requests accepted during post.
                    with self.condition:
                        if self.target is None:
                            self.want_running = False
                    self._state('releasing', owner_held=True)
                    self.board.close()
                    if not self.controller.recovery_confirmed:
                        raise RuntimeError('USB_INDEPENDENT_POST_NOT_CONFIRMED')
                    self._emit('owner_released', recovery_confirmed=True)
                # Finite natural EOS returns to stopped state without implicit replay or reacquisition.
                with self.condition:
                    if self.target is None and (self.backend != 'usb' or self.persistent):
                        self.want_running = False
                    if self.recording:
                        self.releasing = True
        except Exception as fault:
            error = str(fault)
        finally:
            self._state('releasing')
            try:
                self.before_stop()
            except Exception as fault:
                error = error or 'BEFORE_STOP:'+str(fault)
            if self.notification_error:
                error = error or 'NOTIFICATION_FAILED:'+self.notification_error
            if error:
                if self.controller and hasattr(self.controller, 'stop_event'):
                    self.controller.stop_event.set()
                self.graph.stop()
                if self.waiter:
                    self.waiter.join(90)
            try:
                if hasattr(self.board, 'close'):
                    self.board.close()
                else:
                    self.board.shutdown()
                if self.persistent and self.controller.recovery_confirmed:
                    self.controller.paused = False
                if self.controller and getattr(self.controller, 'run_output', None) and not self.controller.recovery_confirmed:
                    raise RuntimeError('GRC_INDEPENDENT_POST_NOT_CONFIRMED')
                if self.waiter and self.waiter.is_alive():
                    raise RuntimeError('GRC_GRAPH_STILL_RUNNING')
            except Exception as fault:
                recovery_error = str(fault)
            self.last_result = dict(status='FAIL' if error or recovery_error else 'PASS_SESSION',
                error=error, recovery_error=recovery_error, device=device_status(self.board), epochs=self.epoch,
                control_evidence=str(getattr(self.controller, 'run_output', '') or ''))
            if self.recording:
                self._state('verifying')
                try:
                    self.last_result['recording'] = self.recording.finish(deepcopy(self.last_result))
                except Exception as fault:
                    error = str(fault)
                    self.last_result.update(status='FAIL', recording_error=error)
            self._emit('finished', result=self.last_result)
            self.failed = self.last_result['status'] == 'FAIL'
            self.releasing = False
            self._state('failed' if error or recovery_error else 'closed' if self.closing else 'released',
                        owner_held=False if not recovery_error else None)

# End of file: grc_session.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
