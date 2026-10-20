#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file live_spectrum.py
@brief Verify native GNU Radio spectrum/waterfall/time plots and preview H102 Ethernet reception.
@details Qt runs on the main thread; GNU C++ carries IQ; the worker handles start, normal stop and
         device recovery. tone is offline; iio is receive-only and requires trusted identity and idle hardware.
         Screenshots capture actual Qt widgets; write new evidence directories without credentials.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-17
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets, sip
from gnuradio import analog, blocks, fft, gr, logicrf, qtgui


def main():
    """Create actual plotting flowgraphs; fail on timeout/native error and independently restore hardware settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('tone', 'iio', 'usb'), default='tone')
    parser.add_argument('--host', default='192.0.2.10')
    parser.add_argument('--machine-id', default=None)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 60:
        parser.error('seconds must be within 5..60')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    project = Path(os.environ['LOGICRF_PROJECT_ROOT'])
    installed = Path(os.environ['LOGICRF_MODULE_PREFIX'])
    board = None
    baseline = None
    backend = None
    connection = None
    if args.backend in ('iio','usb'):
        from gnuradio.logicrf.managed import load_runtime
        backend = load_runtime(project / 'envs/h102-control-runtime/f782657',
                               installed / 'share/gr-logicrf/locks/h102-runtime.lock.json')
        connection = backend.Connection(args.host)
        baseline = backend.inspect(connection)
        (out / 'identity-before.json').write_text(json.dumps(baseline, indent=2))
        if not baseline['actual']['data_idle']:
            raise RuntimeError('BOARD_NOT_IDLE')
        if args.machine_id and baseline['actual']['identity']['machine_id'] != args.machine_id:
            raise RuntimeError('BOARD_IDENTITY_MISMATCH')
        board = logicrf.make_duplex_device(
            backend=args.backend, uri='ip:' + args.host, rx_channels=(1, 2), tx_channels=(),
            configuration='4msps-2mhz-common', sample_rate=4_000_000,
            rx_frequency=1e9, tx_frequency=1e9, rx_gain_db={1: 20, 2: 20},
            rf_enabled=False, sample_limit=0, machine_id=args.machine_id,
            evidence=out / 'control', usb_seconds=0)

    app = QtWidgets.QApplication(sys.argv[:1])
    window = QtWidgets.QWidget()
    window.setWindowTitle('H102 GNU Radio — ' + ('Local plot verification' if board is None else 'Dual RX Ethernet preview'))
    window.resize(1280, 880)
    layout = QtWidgets.QVBoxLayout(window)
    label = QtWidgets.QLabel('Preparing; this program never enables RF TX.')
    layout.addWidget(label)
    rate = 4_000_000 if board else 1_000_000
    center = 1_000_000_000 if board else 0
    freq = qtgui.freq_sink_c(4096, fft.window.WIN_HANN, center, rate, 'RX1 / RX2', 2)
    freq.set_update_time(0.1)
    freq.set_y_axis(-140, 5)
    freq.set_line_label(0, 'RX1' if board else '+62.5 kHz')
    freq.set_line_label(1, 'RX2' if board else '+125 kHz')
    waterfall = qtgui.waterfall_sink_c(1024, fft.window.WIN_HANN, center, rate, 'RX1 waterfall', 1)
    waterfall.set_update_time(0.1)
    waterfall.set_intensity_range(-140, 0)
    temporal = qtgui.time_sink_c(1024, rate, 'IQ time', 2)
    temporal.set_update_time(0.1)
    temporal.set_y_axis(-1, 1)
    for sink in (freq, waterfall, temporal):
        layout.addWidget(sip.wrapinstance(sink.qwidget(), QtWidgets.QWidget))
    graph = gr.top_block()
    probes = [logicrf.stream_probe(False), logicrf.stream_probe(False)]
    # Probe retains the latest vector only; Head propagates DONE upstream and cannot serve a continuous screenshot branch.
    captures = [blocks.probe_signal_vc(65536), blocks.probe_signal_vc(65536)]
    sources = []
    for index in range(2):
        if board:
            source = logicrf.h102_rx_source(board, index + 1)
        else:
            source = blocks.throttle(gr.sizeof_gr_complex, rate, True)
            wave = analog.sig_source_c(rate, analog.GR_COS_WAVE, 62_500 * (index + 1), 0.25, 0)
            sources.append(wave)
            graph.connect(wave, source)
        sources.append(source)
        graph.connect(source, (freq, index))
        graph.connect(source, (temporal, index))
        graph.connect(source, probes[index])
        graph.connect(source, blocks.stream_to_vector(gr.sizeof_gr_complex, 65536), captures[index])
        if index == 0:
            graph.connect(source, waterfall)

    stop = Event()
    done = Event()
    state = {'ready_at': None, 'errors': [], 'baseline_restored': board is None}
    waiter = None

    def worker():
        """Connect/clean up in the worker; retain errors and verify recovery independently from data success."""
        nonlocal waiter
        try:
            graph.start()
            waiter = Thread(target=graph.wait, daemon=True)
            waiter.start()
            deadline = time.monotonic() + 125
            while board and not board.ready():
                if board.error():
                    raise RuntimeError(board.error())
                if not waiter.is_alive() or time.monotonic() > deadline:
                    raise TimeoutError('START_DEADLINE')
                time.sleep(0.03)
            state['ready_at'] = time.monotonic()
            while not stop.wait(0.03) and time.monotonic() - state['ready_at'] < args.seconds:
                if board and board.error():
                    raise RuntimeError(board.error())
                if not waiter.is_alive():
                    raise RuntimeError('UNEXPECTED_FLOWGRAPH_END')
            if board:
                board.stop_rx()
                waiter.join(80)
                if waiter.is_alive():
                    raise TimeoutError('STOP_DEADLINE')
                board.close()
                if board.error() or not board.controller.recovery_confirmed:
                    raise RuntimeError('SOURCE_OR_OWNER_FAILED:' + board.error())
        except Exception as error:
            state['errors'].append(repr(error))
        finally:
            if board and state['errors']:
                try:
                    board.shutdown()
                except Exception as error:
                    state['errors'].append('shutdown:' + repr(error))
            graph.stop()
            if waiter:
                waiter.join(90)
                if waiter.is_alive():
                    state['errors'].append('WAITER_NOT_JOINED')
            if board:
                try:
                    board.close()
                    state['recovery_confirmed'] = board.controller.recovery_confirmed
                    final=backend.inspect(connection)
                    (out/'identity-after.json').write_text(json.dumps(final,indent=2))
                    state['baseline_restored'] = final['actual']['radio']==baseline['actual']['radio'] if final['info']['active']==baseline['info']['active'] else None
                except Exception as error:
                    state['errors'].append('restore:' + repr(error))
            done.set()

    ticks = []
    screenshots = []

    def update():
        """UI heartbeat/screenshots only; never wait for networking or carry IQ in the Qt thread."""
        ticks.append(time.monotonic())
        counts = [int(probe.nitems_read(0)) for probe in probes]
        label.setText(f'{args.backend} | RX1 {counts[0]:,} samples | RX2 {counts[1]:,} samples | RF TX off')
        ready = state['ready_at']
        if ready and not screenshots and time.monotonic() - ready >= 3:
            shot = out / 'qtgui-live.png'
            if window.grab().save(str(shot)):
                screenshots.append(str(shot))
            else:
                state['errors'].append('SCREENSHOT_WRITE_FAILED')
        if done.is_set():
            timer.stop()
            app.quit()

    def close_requested(event):
        """Window close requests managed normal stop, then waits for independent recovery before exiting."""
        event.ignore()
        stop.set()

    window.closeEvent = close_requested
    timer = QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(50)
    window.show()
    thread = Thread(target=worker, daemon=True)
    thread.start()
    app.exec_()
    thread.join(2)
    counts = [int(probe.nitems_read(0)) for probe in probes]
    fft_peaks = []
    for capture in captures:
        data = np.asarray(capture.level(), dtype=np.complex64)
        if len(data) != 65536:
            state['errors'].append('CAPTURE_COUNT_MISMATCH')
            continue
        power = np.abs(np.fft.fft(data * np.hanning(len(data))))
        fft_peaks.append(float(np.fft.fftfreq(len(data), 1 / rate)[int(np.argmax(power))]))
    if not board and fft_peaks != [62_500.0, 125_000.0]:
        state['errors'].append('SYNTHETIC_FFT_PEAK_MISMATCH')
    max_gap = float(max(np.diff(ticks))) if len(ticks) > 1 else None
    if min(counts) <= rate or not screenshots or len(ticks) < 50 or thread.is_alive():
        state['errors'].append('UI_OR_STREAM_NOT_VERIFIED')
    if max_gap is None or max_gap > 0.5:
        state['errors'].append('UI_HEARTBEAT_STALL_OVER_500MS')
    result = dict(status='FAIL' if state['errors'] else 'PASS_SCOPED_LOCAL_QTGUI_' + args.backend.upper(),
                  backend=args.backend, rf_tx=False, state=state, sample_counts=counts,
                  qtgui_samples=[int(freq.nitems_read(i)) for i in range(2)],
                  fft_peaks_hz=fft_peaks, ui_ticks=len(ticks),
                  max_ui_tick_gap_seconds=max_gap, ui_tick_gap_limit_seconds=0.5,
                  screenshots=screenshots, gnuradio=gr.version(), module=str(logicrf.__file__),
                  numpy=np.__version__, screenshot_sha256={p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in screenshots})
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    return int(bool(state['errors']))


if __name__ == '__main__':
    raise SystemExit(main())

# End of file: live_spectrum.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
