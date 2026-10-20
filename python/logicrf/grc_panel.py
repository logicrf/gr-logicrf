"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file grc_panel.py
@brief Embed asynchronous H102 RX controls and bounded native statistics in GRC-generated Qt windows.
@details Create no capture/DSP blocks; users connect actual Source, demodulation, plots and Observer in GRC.
         Window close/SIGINT/SIGTERM await background independent recovery without blocking Qt on hardware transactions.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import signal
import os
from pathlib import Path
from threading import Event, Thread
import uuid
import time
from PyQt5 import QtCore, QtWidgets
from .grc_session import GrcRxSession


class RxPanel(QtWidgets.QWidget):
    """Native GRC controls must be constructed in Qt's main thread; GrcRxSession serializes device transactions.

    Disable GRC Autostart. Supply observers/plots through deferred resolvers so block-name ordering cannot
    access unconstructed later blocks. Resolution itself never contacts devices.
    """
    event_received = QtCore.pyqtSignal(object)

    def __init__(self, graph, board, *, observers=None, displays=None, audio=None, fm=False, recording=None):
        super().__init__(graph)
        graph.setMinimumSize(1100, 800)
        self.graph, self.board = graph, board
        self.observers = observers or (lambda: {})
        self.displays = displays or (lambda: [])
        self.audio = audio or (lambda: None)
        self.fm = fm
        self.recording = recording or (lambda: None)
        self.allow_close = self.closing = False
        self.session = None
        self.last_event = None
        self.scan_worker = None
        self.scan_cancel = Event()
        self.scan_resume = False
        self.last_scan_result = None
        self.scan_requested_at = None
        self.scan_audio_pending = False
        self.query_worker = None
        self.last_query = None
        self.startup_stage = None
        self.startup_clock = None
        self.startup_total = 0.0
        self._old_signals = {}
        self._signal_handler = lambda *_: self.request_close()
        self.event_received.connect(self._event, QtCore.Qt.QueuedConnection)
        layout = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        options = board.options()
        self.identity = QtWidgets.QLabel(f'{options.backend.upper()} · {options.uri or "Simulation"} · '
            f'{options.configuration} · {options.sample_rate/1e6:g} MSPS')
        layout.addWidget(self.identity)
        self.frequency = QtWidgets.QDoubleSpinBox()
        self.frequency.setRange(88 if fm else 50, 108 if fm else 5800)
        self.frequency.setDecimals(6)
        self.frequency.setSingleStep(.1 if fm else 1)
        self.frequency.setSuffix(' MHz')
        self.frequency.setValue(options.center_frequency/1e6)
        self.frequency.setKeyboardTracking(False)
        self.frequency.setToolTip('Press Enter or use the wheel to tune. The frequency axis updates after device readback. Retuning interrupts IQ.')
        self.frequency.valueChanged.connect(self._tune)
        self.query_button = QtWidgets.QPushButton('Device status')
        self.query_button.setEnabled(options.backend != 'mock')
        self.query_button.clicked.connect(self.query)
        row.addWidget(self.query_button)
        row.addWidget(QtWidgets.QLabel('Center frequency'))
        row.addWidget(self.frequency)
        self.start_button = QtWidgets.QPushButton('▶ Start receiving')
        self.start_button.clicked.connect(self.toggle)
        row.addWidget(self.start_button)
        self.release_button = QtWidgets.QPushButton('Disconnect')
        self.release_button.clicked.connect(self.release)
        row.addWidget(self.release_button)
        row.addStretch()
        layout.addLayout(row)
        if fm:
            self.stations = QtWidgets.QListWidget()
            self.stations.setMaximumHeight(90)
            self.stations.setToolTip('Double-click to tune and listen. Search results are spectral candidates, not identified stations.')
            self.stations.itemDoubleClicked.connect(self._station_tune)
            add = QtWidgets.QPushButton('Save current station')
            add.clicked.connect(self._add_station)
            controls = QtWidgets.QHBoxLayout()
            self.scan_button = QtWidgets.QPushButton('Find FM stations')
            self.scan_button.clicked.connect(self.scan)
            self.scan_button.setEnabled(options.backend != 'mock')
            controls.addWidget(self.scan_button)
            for direction, label in ((-1, 'Previous'), (1, 'Next')):
                button = QtWidgets.QPushButton(label)
                button.clicked.connect(lambda checked=False, direction=direction: self.seek(direction))
                controls.addWidget(button)
            controls.addWidget(add)
            layout.addLayout(controls)
            layout.addWidget(self.stations)
        self.status = QtWidgets.QLabel('Not connected. Start receiving to connect. IIO pause keeps the device reserved; USB stop disconnects.')
        self.status.setWordWrap(True)
        self.quality = QtWidgets.QLabel('Time: no samples yet; absolute RF/UTC accuracy is not calibrated')
        self.quality.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(self.quality)
        graph.installEventFilter(self)
        self.close_timer = QtCore.QTimer(self)
        self.close_timer.setInterval(30)
        self.close_timer.timeout.connect(self._poll_close)
        self.progress_timer = QtCore.QTimer(self)
        self.progress_timer.setInterval(200)
        self.progress_timer.timeout.connect(self._startup_status)
        QtCore.QTimer.singleShot(0, self._bind)

    def _bind(self):
        """Bind actual blocks after window construction without device startup or DSP topology changes."""
        if self.session is not None:
            return
        self.session = GrcRxSession(self.graph, self.board, probes=self.observers(),
            notify=self.event_received.emit, before_stop=self._flush_audio, recording=self.recording(),
            health_check=self._audio_health)
        # Generated main installs exit handlers before show; replace them with asynchronous cleanup on the first event-loop turn.
        for number in (signal.SIGINT, signal.SIGTERM):
            self._old_signals[number] = signal.signal(number, self._signal_handler)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._final_join)

    def _flush_audio(self):
        sink = self.audio()
        if sink is not None and hasattr(sink, 'discard_pending'):
            sink.discard_pending()

    def _audio_health(self):
        """Native audio errors/underflows remain visible even if IQ and GUI updates continue normally."""
        sink = self.audio()
        if sink is None or not hasattr(sink, 'snapshot'):
            return {}
        snapshot = sink.snapshot()
        if sink.error() or snapshot.get('underflows', 0):
            raise RuntimeError('FM_AUDIO_HEALTH:'+str(sink.error() or snapshot))
        return dict(audio=snapshot)

    def query(self):
        """Time a read-only identity/Fs/BW query without preparing ownership or changing the selected profile."""
        self._bind()
        if self.board.options().backend == 'mock' or self.closing or self.session.want_running or (
                self.session.state not in ('idle', 'released')) or (
                self.query_worker and self.query_worker.is_alive()):
            return
        self.status.setText('Reading device status…')
        self.query_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.release_button.setEnabled(False)

        def run_query():
            started = time.monotonic()
            try:
                from h102_control.client import Client, Connection
                snapshot = Client(Connection(self.board.controller.host)).request('status', timeout=5)
                value = dict(status='PASS_QUERY', snapshot=snapshot, seconds=time.monotonic()-started)
            except Exception as error:
                value = dict(status='FAIL', error=str(error), seconds=time.monotonic()-started)
            self.event_received.emit(dict(event='query_finished', result=value))

        self.query_worker = Thread(target=run_query, name='h102-grc-query', daemon=False)
        self.query_worker.start()

    def _add_station(self):
        frequency = self.frequency.value()
        if any(self.stations.item(i).data(QtCore.Qt.UserRole) == frequency for i in range(self.stations.count())):
            return
        item = QtWidgets.QListWidgetItem(f'{frequency:.3f} MHz')
        item.setData(QtCore.Qt.UserRole, frequency)
        self.stations.addItem(item)

    def _station_tune(self, item):
        self._bind()
        if self.scan_worker and self.scan_worker.is_alive():
            return
        self.frequency.setValue(item.data(QtCore.Qt.UserRole))
        if not self.session.want_running:
            self.session.start()

    def seek(self, direction):
        """Cycle saved/scanned candidates; search first if none exist rather than inventing stations."""
        self._bind()
        if self.scan_worker and self.scan_worker.is_alive():
            return
        values = sorted(self.stations.item(i).data(QtCore.Qt.UserRole) for i in range(self.stations.count()))
        if not values:
            self.scan()
            return
        current = self.frequency.value()
        choices = [value for value in values if value > current+.00001] if direction > 0 else [value for value in values if value < current-.00001]
        self.frequency.setValue(min(choices) if direction > 0 and choices else max(choices)
                                if choices else min(values) if direction > 0 else max(values))
        if not self.session.want_running:
            self.session.start()

    def scan(self):
        """Borrow warm paused IIO ownership for search; cold/other backends use the existing finite sweep path."""
        self._bind()
        if self.scan_worker and self.scan_worker.is_alive():
            self.scan_cancel.set()
            self.scan_resume = False
            return
        if self.board.options().backend == 'mock' or self.closing:
            return
        self.scan_cancel.clear()
        self.last_scan_result = None
        self.scan_requested_at = time.monotonic()
        self.scan_audio_pending = False
        self.scan_resume = self.session.want_running
        self.start_button.setEnabled(False)
        self.frequency.setEnabled(False)
        self.release_button.setEnabled(False)
        self.scan_button.setText('Cancel search')
        controller = self.board.controller
        warm = bool(self.session.worker and self.session.worker.is_alive() and
                    hasattr(controller,'supports_fast_scan') and controller.supports_fast_scan())
        if warm:self.session.pause()
        else:self.session.release()

        def run_scan():
            try:
                controller = self.board.controller
                root = Path(os.environ.get('LOGICRF_GRC_EVIDENCE_ROOT', controller.evidence))
                output = root/('fm-scan-'+uuid.uuid4().hex)
                progress = lambda value:self.event_received.emit(dict(event='scan_progress', **value))
                if warm:
                    deadline = time.monotonic()+6
                    while self.session.state!='paused':
                        if self.session.failed or self.session.state in ('failed','closed','released') or time.monotonic()>=deadline:
                            raise RuntimeError('FM_SCAN_PAUSE_NOT_VERIFIED')
                        time.sleep(.01)
                    result=controller.scan_stations(output,self.scan_cancel,progress)
                    self.event_received.emit(dict(event='scan_finished',result=result))
                    return
                if not self.session.join(150):
                    raise RuntimeError('FM_SCAN_PREVIOUS_OWNER_NOT_JOINED')
                if self.session.last_result and self.session.last_result['status'] == 'FAIL':
                    raise RuntimeError('FM_SCAN_PREVIOUS_SESSION_FAILED')
                from .fm_tools import scan_stations
                result = scan_stations(controller.host, controller.rx_channels[0],
                    output, self.scan_cancel, progress)
                self.event_received.emit(dict(event='scan_finished', result=result))
            except Exception as error:
                self.event_received.emit(dict(event='scan_finished', result=dict(status='FAIL', error=str(error))))

        self.scan_worker = Thread(target=run_scan, name='h102-grc-fm-scan', daemon=False)
        self.scan_worker.start()

    def _tune(self, value):
        try:
            if self.session:
                self.session.tune(round(value*1e6))
        except Exception as error:
            self.status.setText(str(error))
            if self.session:self.session.reject('tune', error)

    def toggle(self):
        """Buttons submit intent only; completion requires actual worker events."""
        self._bind()
        if self.query_worker and self.query_worker.is_alive():
            return
        try:
            if self.session.want_running:
                self.session.pause()
            else:
                self.session.start()
        except Exception as error:
            self.status.setText(str(error))

    def release(self):
        """Perform normal final recovery without directly destroying the scheduler or killing the owner."""
        self._bind()
        self.session.release()

    @QtCore.pyqtSlot(object)
    def _event(self, value):
        """Qt updates widgets/native Sink axes only, without hardware queries."""
        self.last_event = value
        event = value['event']
        if event == 'accepted':
            names = dict(start='Starting reception', pause='Stopping reception', tune='Tuning; waiting for new-frequency IQ')
            self.status.setText(names.get(value['action'], value['action']))
            if value['action'] == 'start':
                self.startup_stage='identity'
                self.startup_clock=time.monotonic()
                self.startup_total=0.0
                self.progress_timer.start()
            self.query_button.setEnabled(False)
            if value['action'] == 'pause':
                self.start_button.setEnabled(False)
        elif event == 'rejected':
            self.status.setText('Request declined: '+value['error'])
        elif event == 'plan':
            self.status.setText(f'Next frequency: {value["frequency_hz"]/1e6:.6f} MHz; applied when reception starts')
        elif event == 'state':
            names = dict(idle='Idle', preparing='Preparing', running='Receiving', stopping_data='Stopping reception',
                paused='Paused; device reserved by this window', tuning='Tuning', releasing='Restoring settings and disconnecting',
                released='Disconnected', verifying='Checking recording integrity and SHA', failed='Failed; check the error and recovery status', closed='Closed')
            if value['state'] != 'failed':
                self.status.setText(names.get(value['state'], value['state']))
            busy = value['state'] in ('preparing', 'running', 'tuning')
            self.start_button.setText('■ Stop receiving' if busy else '▶ Start receiving')
            if value['state']=='preparing':
                self.start_button.setText('Cancel connection')
            elif value['state'] not in ('tuning',):
                self.progress_timer.stop()
            self.start_button.setEnabled(value['state'] not in ('stopping_data', 'releasing', 'verifying', 'failed', 'closed'))
            self.frequency.setEnabled(not self.session.recording or value['state'] in ('idle', 'released'))
            self.query_button.setEnabled(value['state'] in ('idle', 'released') and self.board.options().backend != 'mock')
            if self.query_worker and self.query_worker.is_alive():
                self.query_button.setEnabled(False)
                self.start_button.setEnabled(False)
                self.release_button.setEnabled(False)
            if self.scan_worker and self.scan_worker.is_alive():
                self.start_button.setEnabled(False)
                self.frequency.setEnabled(False)
            if value['state'] == 'running':
                for sink in self.displays():
                    sink.set_frequency_range(value['frequency_hz'], self.board.options().sample_rate)
                self.status.setText(f'Receiving · First valid IQ: {value["ready_seconds"]:.3f} s · Epoch {value["epoch"]}')
            if self.closing and value['state'] in ('closed', 'failed') and not (
                    self.scan_worker and self.scan_worker.is_alive()) and not (
                    self.query_worker and self.query_worker.is_alive()):
                self.allow_close = True
                self.graph.close()
        elif event == 'startup_stage':
            self.startup_stage=value['stage']
            self.startup_total=value['elapsed_seconds']
            self.startup_clock=time.monotonic()
            self._startup_status()
        elif event == 'stats':
            rows = []
            for channel, observation in value['channels'].items():
                stats = observation['signal']
                quality = dict(relative_only='Relative sample time', invalid='Invalid time',
                    device_digital_valid_utc_uncalibrated='Valid device digital time; UTC accuracy uncalibrated').get(
                        observation['time_quality'], observation['time_quality'])
                rows.append(f'RX{channel}: {observation["samples"]:,} samples · '
                    f'I/Q mean {stats.get("mean_i", 0):.4f}/{stats.get("mean_q", 0):.4f} · '
                    f'RMS {stats.get("rms", 0):.4f} · Peak {stats.get("peak", 0):.4f} · '
                    f'Full scale {stats.get("clipped_fraction", 0):.2%} · {quality}')
            self.quality.setText('\n'.join(rows))
        elif event == 'audio_started':
            self.status.setText(self.status.text()+f' · Audio service ready: {value["audio_service_seconds"]:.3f} s')
            if self.scan_audio_pending and self.last_scan_result is not None:
                self.last_scan_result['end_to_end_audio_seconds'] = value['monotonic']-self.scan_requested_at
                self.last_scan_result['acoustic_latency_verified'] = False
                self.scan_audio_pending = False
        elif event == 'query_finished':
            self.last_query = value['result']
            self.query_button.setEnabled(True)
            self.start_button.setEnabled(not self.session.failed and not self.closing)
            self.release_button.setEnabled(not self.closing)
            if self.last_query['status'] == 'PASS_QUERY':
                snapshot = self.last_query['snapshot']
                parameters = snapshot['info']['parameters']['rx'][0]
                self.status.setText(f'Device status read in {self.last_query["seconds"]:.3f} s · Current profile: {snapshot["info"]["active"]} · '
                    f'{parameters["sample_rate_hz"]/1e6:g} MSPS / {parameters["rf_bandwidth_hz"]/1e6:g} MHz · '
                    'The flowgraph profile is applied when reception starts')
            else:
                self.status.setText('Device status unavailable: '+self.last_query['error'])
        elif event == 'finished' and value['result']['status'] == 'FAIL':
            self.status.setText(f'Failed: {value["result"].get("recording_error") or value["result"]["error"]}; recovery: {value["result"]["recovery_error"] or "Complete"}')
        elif event == 'finished' and value['result'].get('recording'):
            self.quality.setText('Completed recording: ' + value['result']['recording']['manifest'])
        elif event == 'scan_progress':
            self.status.setText(f'FM search: {value["stage"]} {value["completed"]}/{value["total"]}')
        elif event == 'scan_finished':
            result = value['result']
            self.last_scan_result = result
            self.scan_button.setText('Find FM stations')
            self.start_button.setEnabled(result['status'] != 'FAIL')
            self.frequency.setEnabled(result['status'] != 'FAIL')
            self.release_button.setEnabled(True)
            if result['status'] == 'PASS_SPECTRAL_SCAN':
                self.stations.clear()
                for station in result['candidates']:
                    frequency = station['frequency_hz']/1e6
                    item = QtWidgets.QListWidgetItem(f'{frequency:.3f} MHz · Spectral candidate')
                    item.setData(QtCore.Qt.UserRole, frequency)
                    self.stations.addItem(item)
                self.status.setText(f'Search complete: {len(result["candidates"])} candidates · {result["elapsed_seconds"]:.2f} s')
            elif result['status'] == 'FAIL':
                self.status.setText('Search failed: ' + result['error'])
            else:
                self.status.setText('Search cancelled; original settings restored')
            if self.scan_resume and result['status'] != 'FAIL' and not self.closing:
                self.scan_audio_pending = True
                self.session.start()

    def _startup_status(self):
        """Show actual stages/elapsed time without estimated percentages pretending to be hardware progress."""
        if self.startup_clock is None or self.closing:
            return
        if self.session is None or self.session.state!='preparing':
            return
        names=dict(identity='Checking device identity and configuration',profile='Applying sample profile; reinitializing hardware',
                   owner='Starting receive session and checking settings',buffers='Preparing buffers; waiting for valid IQ')
        elapsed=self.startup_total+time.monotonic()-self.startup_clock
        suffix='' if self.session.want_running else ' · Cancellation requested; waiting for the current operation'
        self.status.setText(f'{names.get(self.startup_stage,"Preparing reception")} · {elapsed:.1f} s elapsed{suffix}')

    def eventFilter(self, watched, event):
        if watched is self.graph and event.type() == QtCore.QEvent.Close and not self.allow_close:
            self.request_close()
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def request_close(self):
        """Intercept GRC close/signals and retain the Qt loop until independent recovery finishes."""
        self._bind()
        self.closing = True
        self.scan_cancel.set()
        self.scan_resume = False
        self.session.release(close=True)
        self.close_timer.start()
        self._poll_close()

    def _poll_close(self):
        """Cover the short completion-event/thread-return race without joining hardware transactions in Qt."""
        if self.scan_worker and self.scan_worker.is_alive():
            return
        if self.query_worker and self.query_worker.is_alive():
            return
        if self.session.state in ('closed', 'failed') and not (self.session.worker and self.session.worker.is_alive()):
            self.close_timer.stop()
            self.allow_close = True
            self.graph.close()
        elif self.session.state == 'released' and not (self.session.worker and self.session.worker.is_alive()):
            self.session.release(close=True)

    def _final_join(self):
        """Final normal cleanup on forced Qt exit; SIGKILL cannot run userspace recovery."""
        if self.session:
            self.scan_cancel.set()
            if self.scan_worker:
                self.scan_worker.join(150)
            if self.query_worker:
                self.query_worker.join(20)
            self.session.release(close=True)
            self.session.join(150)
        for number, previous in self._old_signals.items():
            if signal.getsignal(number) is self._signal_handler:
                signal.signal(number, previous)

# End of file: grc_panel.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
