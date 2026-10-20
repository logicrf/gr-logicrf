"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file grc_replay.py
@brief Asynchronous verification/replay between explicit GRC file sources, time tags, throttle and plots.
@details Background checks verify the set's manifest/length/full SHA before playback; native FileSource/H1MI Tagger
         handle data. No device connection; close waits for source completion and verifies unchanged files.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import json
import math
from pathlib import Path
from threading import Event, Thread
from PyQt5 import QtCore, QtWidgets
from gnuradio import blocks, gr
from .logicrf_python import h102_replay_tagger
from .grc_recording import digest


def file_identity(path):
    """Ignore read-only atime changes; device/inode/length/mtime/ctime must remain unchanged."""
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class ReplayPlan:
    """Own declared GRC blocks and one read-only replay; construction has no file IO or network access.

    Workers serialize prepare/verify; register sources/taggers only during Qt flowgraph construction.
    """
    def __init__(self, manifest, channels=(1,)):
        self.manifest = Path(manifest).expanduser().resolve()
        self.channels = tuple(channels)
        if self.channels not in ((1,), (2,), (1, 2)):
            raise ValueError('REPLAY_CHANNELS')
        self.sources, self.taggers, self.fingerprints = {}, {}, {}
        self.record = None

    def source(self, channel):
        """Create native FileSource not yet pointing to user IQ; do not run before prepare."""
        if channel not in self.channels or channel in self.sources:
            raise ValueError('REPLAY_SOURCE_CHANNEL')
        value = blocks.file_source(gr.sizeof_gr_complex, '/dev/zero', False)
        self.sources[channel] = value
        return value

    def tagger(self, channel):
        """Native H1MI restoration block explicitly connected between file source and throttle in GRC."""
        if channel not in self.channels or channel in self.taggers:
            raise ValueError('REPLAY_TAGGER_CHANNEL')
        value = h102_replay_tagger(channel)
        self.taggers[channel] = value
        return value

    def _path(self, filename):
        path = self.manifest.parent/filename
        if path.resolve().parent != self.manifest.parent or path.is_symlink() or not path.is_file():
            raise ValueError('REPLAY_CONFINED_REGULAR_FILE_REQUIRED')
        return path

    def prepare(self, stop=None):
        """Open IQ only after complete verification; failure closes all sources without ignoring indices/corrupt channels."""
        if set(self.sources) != set(self.channels) or set(self.taggers) != set(self.channels):
            raise ValueError('REPLAY_ALL_CHANNELS_REQUIRE_SOURCE_AND_TAGGER')
        if self.manifest.stat().st_size > 2 << 20:
            raise ValueError('REPLAY_MANIFEST_TOO_LARGE')
        record = json.loads(self.manifest.read_text())
        if record.get('schema') != 1 or record.get('status') not in ('PASS_RECORDING', 'PASS_MOCK_RECORDING'):
            raise ValueError('REPLAY_COMPLETED_GRC_RECORDING_REQUIRED')
        if record.get('format') != 'cf32_le' or record.get('backend') not in ('usb', 'iio', 'mock'):
            raise ValueError('REPLAY_FORMAT')
        if any(str(channel) not in record.get('files', {}) for channel in self.channels):
            raise ValueError('REPLAY_CHANNEL_NOT_RECORDED')
        if not math.isfinite(record['rate_hz']) or record['rate_hz'] <= 0:
            raise ValueError('REPLAY_RATE')
        self.record, self.fingerprints = record, {}
        try:
            for name, item in record['files'].items():
                if stop and stop.is_set():
                    raise InterruptedError('REPLAY_CANCELLED_BEFORE_START')
                if int(name) not in (1, 2) or type(item['samples']) is not int or item['samples'] <= 0:
                    raise ValueError('REPLAY_COUNTS')
                if record['backend'] == 'usb' and item['frame_records']*2048 != item['samples']:
                    raise ValueError('REPLAY_INDEX_COVERAGE')
                for field, size, checksum in (('file', item['samples']*8, item['sha256']),
                        ('index_file', item['frame_records']*128, item['index_sha256'])):
                    path = self._path(item[field])
                    before = file_identity(path)
                    if before[2] != size or digest(path, stop) != checksum or file_identity(path) != before:
                        raise ValueError('REPLAY_HASH_SIZE_OR_FILE_CHANGED')
                    self.fingerprints[path] = (before, checksum)
            for channel in self.channels:
                item = record['files'][str(channel)]
                self.sources[channel].open(str(self._path(item['file'])), False, 0, 0)
                info = record['session']['device'].get('last_active_info') or {}
                self.taggers[channel].configure(str(self._path(item['index_file'])), record['backend'],
                    record['rate_hz'], record['frequency_hz'], record['profile'], info.get('boot_id', ''),
                    json.dumps(info, separators=(',', ':')))
        except Exception:
            self.close()
            raise
        return record

    def verify(self, *, cancelled=False, observers=None):
        """After replay check counts/index errors/unchanged originals; cancellation never claims complete EOF success."""
        counts = {}
        for channel in self.channels:
            if self.taggers[channel].error():
                raise RuntimeError(self.taggers[channel].error())
            count = self.taggers[channel].nitems_written(0)
            counts[str(channel)] = count
            if not cancelled and count != self.record['files'][str(channel)]['samples']:
                raise RuntimeError('REPLAY_EOF_COUNT')
        for path, (before, checksum) in self.fingerprints.items():
            if file_identity(path) != before or (not cancelled and digest(path) != checksum):
                raise RuntimeError('REPLAY_FILE_CHANGED')
        observed = {}
        if observers:
            from .grc_session import observation
            if set(observers) != set(self.channels):
                raise RuntimeError('REPLAY_OBSERVER_CHANNELS')
            for channel, probe in observers.items():
                view = observation(probe, self.record['backend'], self.record['rate_hz'])
                stats = view['counters']
                if not cancelled and view['samples'] != counts[str(channel)]:
                    raise RuntimeError('REPLAY_OBSERVER_COUNT')
                if self.record['backend'] == 'usb' and (stats.get('usb_tag_errors', 0) or
                        not cancelled and stats.get('usb_tagged_samples', 0) != counts[str(channel)]):
                    raise RuntimeError('REPLAY_OBSERVED_TAG_COVERAGE')
                observed[str(channel)] = view
        return dict(status='CANCELLED' if cancelled else 'PASS_PLAYBACK', samples=counts,
                    device_access=False, metadata_restored=bool(observed), observed=observed)

    def close(self):
        """Close native sources after scheduler exit without hardware access or recording modification."""
        for source in self.sources.values():
            source.close()


class ReplayPanel(QtWidgets.QWidget):
    """GRC replay control verifies/runs in a worker; Qt updates actual plot/throttle sample rates."""
    event_received = QtCore.pyqtSignal(object)

    def __init__(self, graph, plan, *, throttles, spectra, times, observers=None):
        super().__init__(graph)
        graph.setMinimumSize(1100, 800)
        self.graph, self.plan = graph, plan
        self.throttles, self.spectra, self.times = throttles, spectra, times
        self.observers = observers or (lambda: {})
        self.stop_requested, self.configured = Event(), Event()
        self.worker = None
        self.closing = self.allow_close = False
        self.result = None
        self.description = ''
        self.event_received.connect(self._event, QtCore.Qt.QueuedConnection)
        layout = QtWidgets.QVBoxLayout(self)
        self.button = QtWidgets.QPushButton('▶ Verify and play')
        self.button.clicked.connect(self.toggle)
        self.status = QtWidgets.QLabel(str(plan.manifest))
        self.status.setWordWrap(True)
        layout.addWidget(self.button)
        layout.addWidget(self.status)
        graph.installEventFilter(self)
        QtCore.QTimer.singleShot(0, self._signals)

    def _signals(self):
        import signal
        self._previous_signals = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        self._handler = lambda *_: self.request_close()
        for number in self._previous_signals:
            signal.signal(number, self._handler)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._join)

    def toggle(self):
        """Stop requests cancellation only; reverify every file before replaying again."""
        if self.worker and self.worker.is_alive():
            self.stop_requested.set()
            return
        self.stop_requested.clear()
        self.configured.clear()
        self.worker = Thread(target=self._run, name='h102-grc-replay', daemon=False)
        self.worker.start()
        self.button.setText('■ Stop playback')

    def _run(self):
        waiter = None
        try:
            self.event_received.emit(dict(state='verifying'))
            record = self.plan.prepare(self.stop_requested)
            if self.stop_requested.is_set():
                raise InterruptedError('REPLAY_CANCELLED_BEFORE_START')
            self.event_received.emit(dict(state='configure', record=record))
            if not self.configured.wait(10):
                raise TimeoutError('REPLAY_QT_CONFIGURATION_TIMEOUT')
            if self.stop_requested.is_set():
                raise InterruptedError('REPLAY_CANCELLED_BEFORE_START')
            self.graph.start()
            waiter = Thread(target=self.graph.wait, daemon=True)
            waiter.start()
            self.event_received.emit(dict(state='replaying'))
            while waiter.is_alive() and not self.stop_requested.wait(.02):
                pass
            if self.stop_requested.is_set():
                self.graph.stop()
            waiter.join(10)
            if waiter.is_alive():
                raise RuntimeError('REPLAY_GRAPH_NOT_JOINED')
            self.result = self.plan.verify(cancelled=self.stop_requested.is_set(), observers=self.observers())
        except InterruptedError as error:
            self.result = dict(status='CANCELLED', reason=str(error), device_access=False)
        except Exception as error:
            self.result = dict(status='FAIL', error=str(error), device_access=False)
        finally:
            self.graph.stop()
            if waiter:
                waiter.join(10)
            if waiter and waiter.is_alive():
                self.result = dict(status='FAIL', error='REPLAY_GRAPH_STILL_RUNNING', device_access=False)
            else:
                self.plan.close()
            self.event_received.emit(dict(state='finished', result=self.result))

    def _event(self, value):
        state = value['state']
        self.status.setText({'verifying': 'Checking recording files, index and SHA', 'replaying': 'Playing recording; no device connection'}.get(state, state))
        if state == 'configure':
            record = value['record']
            self.description = f'{record["rate_hz"]/1e6:g} MSPS · {record["frequency_hz"]/1e6:.6f} MHz · {record["backend"].upper()} recording'
            for throttle in self.throttles():
                throttle.set_sample_rate(record['rate_hz'])
            for sink in self.spectra():
                sink.set_frequency_range(record['frequency_hz'], record['rate_hz'])
            for sink in self.times():
                sink.set_samp_rate(record['rate_hz'])
            self.configured.set()
        elif state == 'replaying':
            self.status.setText('Playing · '+self.description)
        elif state == 'finished':
            self.button.setText('▶ Verify and play')
            result = value['result']
            if result['status'] == 'PASS_PLAYBACK':
                counts = ' / '.join(f'RX{channel}: {count:,} samples' for channel, count in result['samples'].items())
                quality = 'Device digital time tags verified; UTC accuracy uncalibrated' if self.plan.record['backend'] == 'usb' else 'Relative sample time'
                self.status.setText(f'Playback complete · {self.description} · {counts} · {quality}')
            elif result['status'] == 'CANCELLED':
                self.status.setText('Playback stopped before the end of the recording')
            else:
                self.status.setText('Playback failed: '+result.get('error', 'Unknown error'))
            if self.closing:
                self.allow_close = True
                self.graph.close()

    def eventFilter(self, watched, event):
        if watched is self.graph and event.type() == QtCore.QEvent.Close and not self.allow_close:
            self.request_close()
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def request_close(self):
        """Close waits for the data thread; verification cancellation is checked at 1 MiB boundaries."""
        self.closing = True
        self.stop_requested.set()
        if not self.worker or not self.worker.is_alive():
            self.allow_close = True
            self.graph.close()

    def _join(self):
        import signal
        self.stop_requested.set()
        if self.worker:
            self.worker.join(150)
        for number, previous in self._previous_signals.items():
            if signal.getsignal(number) is self._handler:
                signal.signal(number, previous)

# End of file: grc_replay.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
