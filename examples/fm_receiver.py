#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file fm_receiver.py
@brief H102 mono FM broadcast receiver with IP connection, spectrum/waterfall, volume and 48 kHz audio.
@details GNU Radio C++ handles IQ/demodulation; Qt handles parameters/status. A managed worker pauses for tuning/search,
         then verifies independent post. --simulate/--self-test avoid devices; actual antenna reception is qualified separately.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.0
@date 2026-10-17
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
from threading import Event,Lock,Thread
import time

from gnuradio import analog,blocks,fft,filter,gr,logicrf

RATE=1_536_000
PROFILE='1p536msps-0p5mhz'


class MonoFM(gr.hier_block2):
    """Convert 1.536 MSPS complex to 192 kSPS FM to 48 kHz mono; select 50/75 us deemphasis explicitly."""
    def __init__(self,tau=50e-6):
        gr.hier_block2.__init__(self,'H102 mono WBFM',gr.io_signature(1,1,gr.sizeof_gr_complex),gr.io_signature(1,1,gr.sizeof_float))
        channel=filter.fir_filter_ccf(8,filter.firdes.low_pass(1,RATE,90_000,6_000,fft.window.WIN_HAMMING))
        demod=analog.quadrature_demod_cf(192_000/(2*math.pi*75_000))
        audio=filter.fir_filter_fff(4,filter.firdes.low_pass(1,192_000,15_000,3_000,fft.window.WIN_HAMMING))
        deemph=analog.fm_deemph(48_000,tau)
        self.connect(self,channel,demod,audio,deemph,self)


def self_test():
    """Check finite synthetic 1 kHz FM audio length, peak and finiteness; do not claim broadcast reception."""
    import numpy as np
    count=RATE//2;t=np.arange(count)/RATE
    modulation=.25*np.sin(2*np.pi*1000*t)
    iq=np.exp(1j*2*np.pi*75_000*np.cumsum(modulation)/RATE).astype(np.complex64)
    graph=gr.top_block();source=blocks.vector_source_c(iq.tolist(),False)
    sink=blocks.vector_sink_f();graph.connect(source,MonoFM(),sink);graph.run()
    values=np.asarray(sink.data());trim=values[2000:-2000]
    spectrum=np.abs(np.fft.rfft(trim*np.hanning(len(trim))))
    peak=np.fft.rfftfreq(len(trim),1/48_000)[np.argmax(spectrum)]
    passed=20_000<=len(values)<=24_000 and bool(np.isfinite(values).all()) and abs(peak-1000)<5 and float(np.std(trim))>.05
    result=dict(status='PASS_OFFLINE' if passed else 'FAIL',audio_samples=len(values),audio_rate_hz=48_000,peak_hz=float(peak),rms=float(np.std(trim)),antenna_test='NOT_RUN')
    print(json.dumps(result));return 0 if passed else 1


def make_window(args):
    """Construct an FM window without hardware effects; serialize hardware work in one worker task.

    Return a Qt window driven by the event loop after show; close cancels and waits for independent recovery.
    Retain only the latest scan/tune request; clear pending work on failure without automatic reconnection.
    """
    from PyQt5 import QtCore, QtWidgets, sip
    from gnuradio import qtgui
    from fm_tools import scan_stations

    class Signals(QtCore.QObject):
        """Workers emit ordinary data only; the Qt main thread exclusively owns and operates widgets."""
        progress = QtCore.pyqtSignal(dict)
        finished = QtCore.pyqtSignal(dict)

    class RadioWindow(QtWidgets.QWidget):
        """One window owns one serial RX/scan job; reuse owners after normal pause only; no new work during final recovery."""
        def __init__(self):
            super().__init__()
            self.stop_event = Event()
            self.tune_lock = Lock()
            self.tune_request = None
            self.audio_sink = None
            self.requested_at = None
            self.worker = None
            self.pending = None
            self.closing = False
            self.job = None
            self.active_frequency = None
            self.board = None
            self.gain = None
            self.last_result = None
            self.results = []
            self.stations = []
            self.signals = Signals()
            self.signals.progress.connect(self.on_progress)
            self.signals.finished.connect(self.on_finished)
            self.setWindowTitle('LogicRF H102 · FM Radio')
            self.resize(1140, 820)
            layout = QtWidgets.QVBoxLayout(self)
            connection_bar = QtWidgets.QHBoxLayout()
            layout.addLayout(connection_bar)
            self.host = QtWidgets.QLineEdit(args.host)
            self.host.setPlaceholderText('H102 device IP')
            self.channel = QtWidgets.QComboBox()
            self.channel.addItems(['RX1A', 'RX2A'])
            self.channel.setCurrentIndex(args.channel - 1)
            self.deemphasis = QtWidgets.QComboBox()
            self.deemphasis.addItems(['50 μs', '75 μs'])
            self.deemphasis.setCurrentIndex(0 if args.deemphasis == 50 else 1)
            for widget in (QtWidgets.QLabel('Device IP'), self.host, self.channel,
                           QtWidgets.QLabel('Deemphasis'), self.deemphasis):
                connection_bar.addWidget(widget)
            self.host.returnPressed.connect(self.toggle)
            bar = QtWidgets.QHBoxLayout()
            layout.addLayout(bar)
            self.station = QtWidgets.QDoubleSpinBox()
            self.station.setRange(88, 108)
            self.station.setDecimals(3)
            self.station.setSingleStep(.1)
            self.station.setSuffix(' MHz')
            self.station.setValue(args.frequency)
            self.station.setKeyboardTracking(False)
            self.previous = QtWidgets.QPushButton('◀ Previous')
            self.next = QtWidgets.QPushButton('Next ▶')
            self.apply = QtWidgets.QPushButton('Tune')
            self.start = QtWidgets.QPushButton('▶ Start')
            self.scan = QtWidgets.QPushButton('Find stations')
            for widget in (self.station, self.apply, self.previous, self.next, self.start, self.scan):
                bar.addWidget(widget)
            sound = QtWidgets.QHBoxLayout()
            layout.addLayout(sound)
            self.mute = QtWidgets.QCheckBox('Mute')
            self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.volume.setRange(0, 100)
            self.volume.setValue(40)
            self.volume_label = QtWidgets.QLabel('40%')
            sound.addWidget(self.mute)
            sound.addWidget(QtWidgets.QLabel('Volume'))
            sound.addWidget(self.volume)
            sound.addWidget(self.volume_label)
            self.status = QtWidgets.QLabel('Ready · RX only · Mono · Tuning/search briefly pauses reception')
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.progress_bar = QtWidgets.QProgressBar()
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            layout.addWidget(self.progress_bar)
            split = QtWidgets.QSplitter()
            layout.addWidget(split, 1)
            plots = QtWidgets.QWidget()
            plot_layout = QtWidgets.QVBoxLayout(plots)
            plot_layout.setContentsMargins(0, 0, 0, 0)
            self.spectrum = qtgui.freq_sink_c(4096, fft.window.WIN_HANN, args.frequency * 1e6, RATE, 'FM RF spectrum', 1)
            self.spectrum.set_y_axis(-140, 5)
            self.spectrum.set_update_time(.1)
            self.waterfall = qtgui.waterfall_sink_c(1024, fft.window.WIN_HANN, args.frequency * 1e6, RATE, 'FM waterfall', 1)
            self.waterfall.set_update_time(.1)
            self.waterfall.set_intensity_range(-140, 0)
            for plot in (self.spectrum, self.waterfall):
                plot_layout.addWidget(sip.wrapinstance(plot.qwidget(), QtWidgets.QWidget))
            split.addWidget(plots)
            panel = QtWidgets.QWidget()
            panel.setMinimumWidth(220)
            panel_layout = QtWidgets.QVBoxLayout(panel)
            panel_layout.addWidget(QtWidgets.QLabel('Station candidates'))
            self.station_list = QtWidgets.QListWidget()
            self.station_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            panel_layout.addWidget(self.station_list, 1)
            self.save_list = QtWidgets.QPushButton('Save station list…')
            self.load_list = QtWidgets.QPushButton('Load station list…')
            panel_layout.addWidget(self.save_list)
            panel_layout.addWidget(self.load_list)
            split.addWidget(panel)
            split.setSizes([850, 240])
            self.start.clicked.connect(self.toggle)
            self.apply.clicked.connect(self.request_tune)
            self.station.editingFinished.connect(self.request_tune)
            self.previous.clicked.connect(lambda: self.seek(-1))
            self.next.clicked.connect(lambda: self.seek(1))
            self.scan.clicked.connect(self.request_scan)
            self.station_list.itemDoubleClicked.connect(self.select_station)
            self.volume.valueChanged.connect(self.update_volume)
            self.mute.toggled.connect(self.update_volume)
            self.save_list.clicked.connect(self.export_stations)
            self.load_list.clicked.connect(self.import_stations)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(100)

        def update_volume(self, *unused):
            """Volume/mute changes the audio multiplier only, without altering IQ, RF gain or system mixer."""
            self.volume_label.setText(str(self.volume.value()) + '%')
            if self.gain is not None:
                self.gain.set_k(0 if self.mute.isChecked() else self.volume.value() / 100)

        def schedule(self, job):
            """Coalesce IIO retunes under the same owner; scan/USB jobs wait for complete recovery."""
            if self.worker is not None:
                if job['kind'] == 'rx' and self.job['kind'] == 'rx' and not self.stop_event.is_set():
                    if job['frequency_hz'] == self.job['frequency_hz']:
                        return
                    if self.board is not None and hasattr(self.board.controller, 'retune'):
                        with self.tune_lock:
                            self.tune_request = dict(job, requested_at=time.monotonic())
                        self.job = dict(job)
                        self.status.setText('Tuning…')
                        if self.audio_sink is not None: self.audio_sink.discard_pending()
                        return
                self.pending = job
                self.stop_event.set()
                if self.audio_sink is not None: self.audio_sink.discard_pending()
                self.status.setText('Stopping and confirming recovery before the latest request…')
            else:
                self.launch(job)

        def toggle(self):
            """Start/cancel the current task; an explicit stop clears pending tuning/search."""
            if self.worker is not None:
                if self.stop_event.is_set():
                    self.pending = None if self.pending else dict(kind='rx', frequency_hz=round(self.station.value()*1e6))
                    self.start.setText('■ Cancel start' if self.pending else '▶ Start')
                    self.status.setText('Reception starts after device release…' if self.pending else 'Releasing device…')
                    return
                self.pending = None
                self.stop_event.set()
                if self.audio_sink is not None: self.audio_sink.discard_pending()
                self.start.setText('▶ Start')
                self.status.setText('Stopping playback and releasing device…')
            else:
                self.schedule(dict(kind='rx', frequency_hz=round(self.station.value() * 1e6)))

        def request_tune(self):
            """Pause/retune during reception/preparation; when idle, store the next frequency only."""
            hz = round(self.station.value() * 1e6)
            if self.worker is not None and self.job['kind'] == 'rx' and hz != self.job['frequency_hz']:
                self.schedule(dict(kind='rx', frequency_hz=hz))
            elif self.worker is None:
                self.status.setText(f'Selected {hz/1e6:.3f} MHz · Start reception to apply')

        def seek(self, direction):
            """Cycle adjacent listed stations, or step 100 kHz without claiming an automatically detected station."""
            hz = round(self.station.value() * 1e6)
            frequencies = sorted({row['frequency_hz'] for row in self.stations})
            choices = [value for value in frequencies if (value - hz) * direction > 10]
            if frequencies:
                target = (min(choices) if direction > 0 else max(choices)) if choices else (frequencies[0] if direction > 0 else frequencies[-1])
            else:
                target = hz + direction * 100_000
                if target < 88_000_000: target = 108_000_000
                if target > 108_000_000: target = 88_000_000
            self.station.setValue(target / 1e6)
            self.request_tune()

        def request_scan(self):
            """Finite Ethernet scan only; resume the original playback frequency after recovery; remain stopped on failure."""
            if args.simulate:
                self.status.setText('Simulation does not access hardware; station search is unavailable')
                return
            if self.worker is not None and self.job['kind'] == 'scan':
                self.pending = None
                self.stop_event.set()
                self.status.setText('Cancelling search and restoring…')
                return
            resume = self.job is not None and self.job['kind'] == 'rx' and self.worker is not None
            self.schedule(dict(kind='scan', resume=resume, frequency_hz=round(self.station.value() * 1e6)))

        def select_station(self, item):
            """Double-click explicitly selects a candidate; retune if playing, otherwise start."""
            self.station.setValue(item.data(QtCore.Qt.UserRole) / 1e6)
            self.schedule(dict(kind='rx', frequency_hz=round(self.station.value() * 1e6)))

        def set_stations(self, stations):
            """Show verified frequencies/finite power only, without invented programs or station names."""
            self.stations = stations
            self.station_list.clear()
            for row in stations:
                item = QtWidgets.QListWidgetItem(f"{row['frequency_hz']/1e6:.1f} MHz  ·  Δ{row.get('contrast_db', 0):.1f} dB")
                item.setData(QtCore.Qt.UserRole, row['frequency_hz'])
                item.setToolTip('Spectral candidate; double-click to listen. Not FM/RDS identity confirmation.')
                self.station_list.addItem(item)

        def export_stations(self):
            """Explicitly save JSON frequencies without addresses, credentials, IQ or audio."""
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save station list', 'h102-fm-stations.json', 'JSON (*.json)')
            if path:
                try:
                    Path(path).write_text(json.dumps(dict(schema=1, band='FM88-108MHz', stations=self.stations), ensure_ascii=False, indent=2) + '\n')
                except Exception as error:
                    self.status.setText('Save failed: ' + str(error))

        def import_stations(self):
            """Bound file size/count/frequencies; failure preserves the current list and never triggers hardware."""
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Load station list', '', 'JSON (*.json)')
            if path:
                try:
                    if Path(path).stat().st_size > 65536: raise ValueError('Station list is too large')
                    value = json.loads(Path(path).read_text())
                    rows = value['stations']
                    if value.get('schema') != 1 or not isinstance(rows, list) or len(rows) > 64: raise ValueError('Invalid station list format')
                    clean = []
                    for row in rows:
                        hz, contrast = row['frequency_hz'], float(row.get('contrast_db', 0))
                        if type(hz) is not int or not 88_000_000 <= hz <= 108_000_000 or not math.isfinite(contrast): raise ValueError('Invalid frequency/strength')
                        clean.append(dict(frequency_hz=hz, contrast_db=contrast, qualification='imported_candidate'))
                    self.set_stations(sorted(clean, key=lambda row: row['frequency_hz']))
                except Exception as error:
                    self.status.setText('Load failed: ' + str(error))

        def launch(self, job):
            """Qt constructs graph/widgets; worker starts/stops/captures/restores and emits results only."""
            if not args.simulate:
                try:
                    from h102_control.client import Connection
                    Connection(self.host.text().strip()).validate()
                except ValueError:
                    self.pending = None
                    self.status.setText('Enter a valid H102 device IP')
                    return
            args.host = self.host.text().strip()
            args.channel = self.channel.currentIndex() + 1
            args.deemphasis = 50 if self.deemphasis.currentIndex() == 0 else 75
            for field in (self.host, self.channel, self.deemphasis): field.setEnabled(False)
            self.stop_event.clear()
            with self.tune_lock: self.tune_request = None
            self.requested_at = time.monotonic()
            self.job = dict(job)
            self.active_frequency = None
            self.board = None
            self.gain = None
            self.start.setText('■ Stop')
            self.scan.setText('Cancel search' if job['kind'] == 'scan' else 'Find stations')
            self.progress_bar.setVisible(job['kind'] == 'scan')
            self.status.setText('Preparing…')
            if job['kind'] == 'scan':
                def execute_scan():
                    try:
                        root = Path(os.environ['LOGICRF_PROJECT_ROOT']) / 'evidence' / ('fm-scan-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.monotonic_ns()))
                        result = scan_stations(args.host, args.channel, root, self.stop_event, self.signals.progress.emit)
                        result['evidence_directory'] = str(root)
                    except Exception as error:
                        result = dict(status='FAIL', error=repr(error))
                    self.signals.finished.emit(result)
                self.worker = Thread(target=execute_scan, daemon=False)
            else:
                hz = job['frequency_hz']
                self.spectrum.reset()
                self.waterfall.clear_data()
                graph = gr.top_block()
                if args.simulate:
                    tone = analog.sig_source_f(192_000, analog.GR_SIN_WAVE, 1000, .25)
                    modulator = analog.frequency_modulator_fc(2 * math.pi * 75_000 / 192_000)
                    interpolation = filter.rational_resampler_ccc(interpolation=8, decimation=1)
                    source = blocks.throttle(gr.sizeof_gr_complex, RATE, True)
                    graph.connect(tone, modulator, interpolation, source)
                else:
                    if args.backend == 'iio':
                        from gnuradio.logicrf.fm_managed import make_fm_device
                        self.board = make_fm_device(stop_event=self.stop_event, channel=args.channel,
                            uri='ip:' + args.host, configuration=PROFILE, sample_rate=RATE,
                            rx_frequency=hz, tx_frequency=hz, tx_samples=0, usb_seconds=0)
                    else:
                        self.board = logicrf.make_duplex_device(backend=args.backend, uri='ip:' + args.host, configuration=PROFILE,
                            sample_rate=RATE, rx_channels=(args.channel,), tx_channels=(), rx_frequency=hz,
                            tx_frequency=hz, rf_enabled=False, rx_gain_db={args.channel:20}, tx_samples=0, usb_seconds=0)
                    source = logicrf.h102_rx_source(self.board, args.channel)
                self.gain = blocks.multiply_const_ff(0 if self.mute.isChecked() else self.volume.value() / 100)
                if args.no_audio:
                    sink = blocks.null_sink(gr.sizeof_float)
                else:
                    sink = logicrf.fm_audio_sink(1000)
                demod = MonoFM(args.deemphasis * 1e-6)
                self.audio_sink = sink if hasattr(sink, 'discard_pending') else None
                graph.connect(source, demod, self.gain, sink)
                graph.connect(source, self.spectrum)
                graph.connect(source, self.waterfall)
                self.worker = Thread(target=self.receive, args=(graph, self.board, hz, sink, source, demod), daemon=False)
            self.worker.start()

        def receive(self, graph, board, hz, sink, source, demod):
            """Retune serially under one owner; end guard/destroy buffers per generation, restore original settings at final exit."""
            result = dict(status='RUNNING', frequency_hz=hz, RF_TX=False, epochs=[])
            waiter = None
            requested = self.requested_at
            try:
                while True:
                    graph.start()
                    waiter = Thread(target=graph.wait, daemon=True)
                    waiter.start()
                    deadline = time.monotonic() + 125
                    while board and not board.ready():
                        if self.stop_event.is_set(): raise InterruptedError('FM_PREPARE_CANCELLED')
                        if hasattr(sink, 'error') and sink.error(): raise RuntimeError(sink.error())
                        if board.error(): raise RuntimeError(board.error())
                        if not waiter.is_alive() or time.monotonic() > deadline: raise TimeoutError('FM_START_DEADLINE')
                        time.sleep(.01)
                    ready = time.monotonic()
                    self.signals.progress.emit(dict(stage='receiving', frequency_hz=hz))
                    print(json.dumps(dict(event='FM_READY', frequency_hz=hz, seconds=ready-requested)), flush=True)
                    epoch = dict(frequency_hz=hz, request_monotonic=requested,
                                 ready_seconds=ready-requested, audio_started_seconds=None)
                    target = None
                    while not self.stop_event.wait(.01) and (not args.seconds or time.monotonic()-ready < args.seconds):
                        if hasattr(sink, 'error') and sink.error(): raise RuntimeError(sink.error())
                        if board and board.error(): raise RuntimeError(board.error())
                        if not waiter.is_alive(): raise RuntimeError('FM_UNEXPECTED_END')
                        if hasattr(sink, 'snapshot'):
                            audio = sink.snapshot()
                            if audio['playback_started'] and epoch['audio_started_seconds'] is None:
                                epoch['audio_started_seconds'] = time.monotonic()-requested
                        with self.tune_lock:
                            target, self.tune_request = self.tune_request, None
                        if target is not None: break
                    stopped = time.monotonic()
                    # Explicit interaction aborts old audio immediately; finite EOF checks still drain by default.
                    if (self.stop_event.is_set() or target is not None) and hasattr(sink, 'discard_pending'):
                        sink.discard_pending()
                    if target is not None:
                        board.controller.hold_for_tune = True
                        self.signals.progress.emit(dict(stage='tuning', frequency_hz=target['frequency_hz']))
                    if board: board.stop_rx()
                    else: graph.stop()
                    waiter.join(90)
                    if waiter.is_alive(): raise TimeoutError('FM_STOP_DEADLINE')
                    if hasattr(sink, 'snapshot'):
                        epoch['audio_output'] = sink.snapshot()
                        if epoch['audio_output']['playback_started'] and epoch['audio_started_seconds'] is None:
                            epoch['audio_started_seconds'] = stopped-requested
                        if sink.error() or epoch['audio_output']['underflows']:
                            raise RuntimeError('FM_AUDIO_NOT_STABLE:' + sink.error())
                    if board:
                        epoch['device'] = logicrf.device_status(board)
                    epoch.update(stop_to_graph_end_seconds=time.monotonic()-stopped,
                                 receiving_seconds=stopped-ready)
                    result['epochs'].append(epoch)
                    if target is None or self.stop_event.is_set(): break
                    with self.tune_lock:
                        if self.tune_request is not None:
                            target, self.tune_request = self.tune_request, None
                    hz, requested = target['frequency_hz'], target['requested_at']
                    board.controller.retune(hz)
                    # Source enter clears queues/indices; rebuild demodulation/deemphasis filters so old state cannot cross stations.
                    graph.disconnect(source, demod)
                    graph.disconnect(demod, self.gain)
                    demod = MonoFM(args.deemphasis * 1e-6)
                    graph.connect(source, demod, self.gain)
                if board:
                    board.close()
                    if board.error() or not board.controller.recovery_confirmed:
                        raise RuntimeError('FM_RECOVERY_UNCONFIRMED:' + board.error())
                    result['device'] = logicrf.device_status(board)
                if hasattr(sink, 'snapshot'): result['audio_output'] = sink.snapshot()
                result.update(status='PASS_SESSION', receiving_seconds=sum(x['receiving_seconds'] for x in result['epochs']),
                              stop_to_released_seconds=time.monotonic()-stopped,
                              antenna_test='NOT_ESTABLISHED_BY_THIS_RUN')
            except Exception as error:
                result.update(status='FAIL', error=repr(error))
                if hasattr(sink, 'discard_pending'): sink.discard_pending()
                graph.stop()
                if waiter: waiter.join(90)
                if board:
                    try: board.close()
                    except Exception as recovery: result['recovery_error'] = repr(recovery)
                    result['device'] = logicrf.device_status(board)
                if self.stop_event.is_set() and ('FM_PREPARE_CANCELLED' in repr(error) or
                        'START_CANCELLED' in repr(error) or
                        board and getattr(board.controller, 'cancelled_preparation', False)) and (not board or board.controller.recovery_confirmed or
                        not getattr(board.controller, 'requires_recovery', lambda: True)()):
                    result['status'] = 'CANCELLED'
            self.signals.finished.emit(result)

        def on_progress(self, value):
            """Separate actual readiness from search progress; a requested target is not hardware readback."""
            stage = value['stage']
            if stage == 'receiving':
                if self.stop_event.is_set(): return
                self.active_frequency = value['frequency_hz']
                self.spectrum.set_frequency_range(self.active_frequency, RATE)
                self.waterfall.set_frequency_range(self.active_frequency, RATE)
                self.status.setText(f"Receiving {self.active_frequency/1e6:.3f} MHz · Mono 48 kHz · " + ('Synthetic signal' if args.simulate else args.host))
            elif stage == 'tuning':
                self.active_frequency = None
                self.spectrum.reset()
                self.waterfall.clear_data()
                self.status.setText('Tuning…')
            else:
                self.progress_bar.setRange(0, value['total'])
                self.progress_bar.setValue(value['completed'])
                self.status.setText({'preparing':'Preparing station search…', 'restoring':'Verifying profile and LO restoration…'}.get(stage,
                    f"Searching {value['completed']}/{value['total']} · Cancellable"))

        def on_finished(self, result):
            """Run queued work only after the worker exits completely; never resume playback automatically after failure."""
            self.worker.join()
            self.worker = None
            self.audio_sink = None
            self.gain = None
            self.active_frequency = None
            self.last_result = result
            self.results.append(result)
            self.start.setText('▶ Start')
            self.scan.setText('Find stations')
            self.progress_bar.setVisible(False)
            failed = result['status'] == 'FAIL'
            if self.job['kind'] == 'scan' and result['status'] == 'PASS_SPECTRAL_SCAN':
                self.set_stations(result['candidates'])
                self.status.setText(f"Search complete: {len(self.stations)} spectral candidates; double-click to listen · {result['elapsed_seconds']:.1f} s including preparation/recovery")
            elif result['status'] == 'CANCELLED':
                self.status.setText('Operation cancelled; device released')
            else:
                detail = str(result.get('error', result.get('recovery_error', 'Unknown error')))
                self.status.setText(('Operation failed: ' + detail[:180]) if failed else 'Stopped; recovery confirmed')
                self.status.setToolTip(detail if failed else '')
            if args.output:
                target = args.output if len(self.results) == 1 else args.output.with_name(args.output.stem + f'-{len(self.results):03}' + args.output.suffix)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('x') as stream: json.dump(result, stream, ensure_ascii=False, indent=2)
                except Exception as error: self.status.setText('Cannot save results: ' + str(error))
            if failed:
                self.pending = None
            elif self.job['kind'] == 'scan' and self.job.get('resume') and self.pending is None and result['status'] != 'CANCELLED':
                self.pending = dict(kind='rx', frequency_hz=self.job['frequency_hz'])
            for field in (self.host, self.channel, self.deemphasis): field.setEnabled(self.pending is None)
            if self.closing or (args.autostart and self.pending is None):
                self.close()
            elif self.pending is not None:
                job, self.pending = self.pending, None
                QtCore.QTimer.singleShot(0, lambda: self.launch(job))

        def tick(self):
            """Short Qt ticks handle screenshots only; acquisition/search/integrity do not depend on GUI frame rate."""
            if args.output and self.active_frequency and self.worker and not getattr(self, 'saved_screenshot', False):
                image = args.output.with_suffix('.png')
                if not image.exists() and self.grab().save(str(image)): self.saved_screenshot = True

        def closeEvent(self, event):
            """Close revokes future work immediately and waits for the current task and independent recovery."""
            self.pending = None
            if self.worker is not None:
                self.closing = True
                self.stop_event.set()
                if self.audio_sink is not None: self.audio_sink.discard_pending()
                self.status.setText('Releasing device before closing…')
                event.ignore()
            else:
                event.accept()

    return RadioWindow()


def main():
    """Start an IP-only FM receiver; seconds/output support finite acceptance checks; RF TX always stays off."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='')
    parser.add_argument('--backend', choices=('iio', 'usb'), default='iio')
    parser.add_argument('--channel', type=int, choices=(1, 2), default=1)
    parser.add_argument('--frequency', type=float, default=98.1, help='FM station MHz')
    parser.add_argument('--deemphasis', type=int, choices=(50, 75), default=50, help='microseconds')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--no-audio', action='store_true')
    parser.add_argument('--seconds', type=float, default=0)
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.self_test: return self_test()
    if not 88 <= args.frequency <= 108 or args.seconds < 0: parser.error('frequency 88..108 MHz; seconds >= 0')
    if args.autostart and not args.simulate and not args.host: parser.error('--autostart requires --host DEVICE_IP')
    from PyQt5 import QtCore, QtWidgets
    app = QtWidgets.QApplication(sys.argv[:1])
    window = make_window(args)
    window.show()
    if args.autostart: QtCore.QTimer.singleShot(0, window.toggle)
    app.exec_()
    return 1 if window.last_result and window.last_result['status'] == 'FAIL' else 0


if __name__ == '__main__': raise SystemExit(main())
# End of file: fm_receiver.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
