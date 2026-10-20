"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file grc_recording.py
@brief Unique recording directories, integrity checks and atomic completion manifests for native GRC branches.
@details C++ writes cf32/H1MI; background Python checks space, counts, guards and full SHA only.
         Retain partial status until success and failure reasons afterward; do not claim unobservable absolute ADC indices.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from gnuradio import blocks, gr


def digest(path, cancel=None):
    """Stream full SHA within 1 MiB memory; optional cancellation raises InterruptedError at block boundaries."""
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            if cancel is not None and cancel.is_set():
                raise InterruptedError('FILE_VERIFICATION_CANCELLED')
            checksum.update(chunk)
    return checksum.hexdigest()


def publish(path, value):
    """Atomically publish JSON and fsync its directory; the completion manifest commits the entire set."""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RecordingSink(gr.hier_block2):
    """Reusable native file/index endpoints without Python work; construction opens /dev/null only.

    RecordingPlan selects separate paths before graph start; register one block per declared channel.
    """
    def __init__(self, plan, channel):
        from . import h102_metadata_sink
        gr.hier_block2.__init__(self, 'h102_recording_sink',
            gr.io_signature(1, 1, gr.sizeof_gr_complex), gr.io_signature(0, 0, 0))
        self.file = blocks.file_sink(gr.sizeof_gr_complex, '/dev/null', False)
        self.index = h102_metadata_sink(plan.board, '/dev/null/h102-not-started', channel)
        self.connect(self, self.file)
        self.connect(self, self.index)


class RecordingPlan:
    """One complete recording set per RX session; a background session thread serializes all lifecycle methods.

    User supplies root; each begin creates a unique child directory without overwrite; construction creates none.
    """
    def __init__(self, board, root, channels=(1, 2)):
        if sys.byteorder != 'little':
            raise RuntimeError('CF32_LE_HOST_REQUIRED')
        self.board = board
        self.root = Path(root).expanduser().resolve()
        self.channels = tuple(channels)
        if self.channels not in ((1,), (2,), (1, 2)):
            raise ValueError('RECORD_CHANNELS')
        self.sinks = {}
        self.directory = None
        self.result = None

    def sink(self, channel):
        """Create native endpoints for declared channels; reject duplicates/undeclared channels immediately."""
        if channel not in self.channels or channel in self.sinks:
            raise ValueError('RECORD_SINK_CHANNEL')
        result = RecordingSink(self, channel)
        self.sinks[channel] = result
        return result

    def begin(self):
        """Check all channels/space before start; open cf32/index files only within this recording's directory."""
        self.directory = None
        self.result = None
        if set(self.sinks) != set(self.channels):
            raise RuntimeError('RECORD_ALL_CHANNELS_REQUIRE_SINKS')
        self.root.mkdir(parents=True, exist_ok=True)
        self.check_space(self.root)
        self.directory = self.root/('capture-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12])
        self.directory.mkdir(mode=0o700, exist_ok=False)
        self.result = None
        options = self.board.options()
        publish(self.directory/'request.json', dict(schema=1, status='RUNNING', format='cf32_le',
            frame_index='H1MI-v1-128', backend=options.backend, rate_hz=options.sample_rate,
            profile=options.configuration, frequency_hz=options.center_frequency,
            channels=self.channels, RF_TX=False))
        for channel, sink in self.sinks.items():
            if not sink.file.open(str(self.directory/f'rx{channel}.cf32.partial')):
                raise RuntimeError('RECORD_FILE_OPEN')
            sink.index.set_path(str(self.directory/f'rx{channel}.h1mi.partial'))

    def check_space(self, path):
        """Reserve at least 1 GiB or five seconds of cf32; insufficient space stops explicitly without deleting old data."""
        needed = max(1 << 30, int(self.board.options().sample_rate*len(self.channels)*8*5))
        if shutil.disk_usage(path).free < needed:
            raise RuntimeError('RECORD_DISK_SPACE_LOW')

    def check(self):
        """Bound active checks; index errors/low space request normal stop and failed sealing."""
        if self.directory:
            self.check_space(self.directory)
        if any(sink.index.error() for sink in self.sinks.values()):
            raise RuntimeError('RECORD_INDEX_IO')

    def finish(self, session_result):
        """After independent recovery, verify the full set; rename/publish completion only after every file passes.

        Retain partial files/FAIL manifest on failure; file SHA alone cannot establish ADC continuity.
        """
        if self.directory is None:
            return None
        result = dict(schema=1, status='FAIL', format='cf32_le', frame_index='H1MI-v1-128',
                      session=session_result, files={}, RF_TX=False)
        try:
            for sink in self.sinks.values():
                sink.file.close()
            if session_result['status'] != 'PASS_SESSION':
                raise RuntimeError('RECORD_SESSION_FAILED')
            options = self.board.options()
            control = getattr(self.board, 'controller', None)
            result.update(backend=options.backend, profile=options.configuration,
                rate_hz=options.sample_rate, frequency_hz=options.center_frequency,
                sample_index_kind='host_output_not_adc' if options.backend == 'iio' else
                    'native_adc_frame_index' if options.backend == 'usb' else 'synthetic',
                time_quality='device_digital_valid_utc_uncalibrated' if options.backend == 'usb' else 'relative_only')
            if control and not control.recovery_confirmed:
                raise RuntimeError('RECORD_POST_NOT_CONFIRMED')
            for channel, sink in self.sinks.items():
                count = self.board.received(channel)
                path = self.directory/f'rx{channel}.cf32.partial'
                index = self.directory/f'rx{channel}.h1mi.partial'
                if not count or count != sink.file.nitems_read(0) or path.stat().st_size != count*8:
                    raise RuntimeError('RECORD_COUNT_OR_SIZE')
                records = sink.index.records()
                if sink.index.error() or index.stat().st_size != records*128 or sink.index.nitems_read(0) != count:
                    raise RuntimeError('RECORD_INDEX_SIZE')
                if options.backend == 'usb' and records*2048 != count:
                    raise RuntimeError('RECORD_INDEX_COVERAGE')
                if options.backend == 'iio' and not control.guard_records.get('rx'+str(channel), {}).get('end', {}).get('passed'):
                    raise RuntimeError('RECORD_ADC_GUARD')
                for file in (path, index):
                    with file.open('rb') as stream:
                        os.fsync(stream.fileno())
                result['files'][str(channel)] = dict(samples=count, bytes=count*8, file=f'rx{channel}.cf32',
                    sha256=digest(path), index_file=f'rx{channel}.h1mi', frame_records=records,
                    index_sha256=digest(index))
            if options.backend == 'usb':
                result['stop_boundary'] = dict(adc_sample_exact_endpoint=False,
                    kind='complete_frames_before_protocol_stop', wire_partial_tail_bytes=max(
                        self.board.queue_stats(ch).get('usb_partial_tail_bytes', 0) for ch in self.channels))
            for channel in self.channels:
                for suffix in ('cf32', 'h1mi'):
                    partial = self.directory/f'rx{channel}.{suffix}.partial'
                    partial.rename(partial.with_suffix(''))
            result['status'] = 'PASS_MOCK_RECORDING' if options.backend == 'mock' else 'PASS_RECORDING'
        except Exception as error:
            result['error'] = str(error)
        publish(self.directory/'recording.json', result)
        self.result = result
        if result['status'] == 'FAIL':
            raise RuntimeError(result['error'])
        return dict(status=result['status'], manifest=str(self.directory/'recording.json'))

# End of file: grc_recording.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
