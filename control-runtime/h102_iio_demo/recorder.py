"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       recorder.py
@brief      Write complete per-channel raw IQ and verifiable receipts in an independent thread.
@details    Fail immediately on a full bounded queue without silent drops; drain/fsync and verify bytes/hashes at close.
            Device sticky-overflow windows plus complete file writes qualify the scoped ADC-to-DMA-to-file interval; failures retain .partial.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.8.0
@date       2026-10-14
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Queue, Full, Empty
import shutil
from threading import Event, Thread, Lock
import time


def probe_disk_rate(directory):
    """An 8 MiB write/fsync admission probe in the target directory, not sustained throughput or stall qualification."""
    probe = Path(directory) / 'write-probe.tmp'
    start = time.monotonic()
    with probe.open('xb', buffering=0) as stream:
        stream.write(bytes(8 * 1024**2))
        os.fsync(stream.fileno())
    elapsed = time.monotonic() - start
    rate = probe.stat().st_size / elapsed
    probe.unlink()
    return rate


class IQRecorder:
    """Multiple producers, one writer; blocks at most 1 MiB with up to four seconds of input buffering.

    submit accepts complete LE IQ16 only. Stop and join producers before finish;
    the queue holds 64-256 blocks with a hard 256 MiB limit, not unlimited RAM recording.
    Construct and finish in a worker thread; failures never delete received data.
    """
    def __init__(self, directory, channels, info, required_rate, seconds=None, source="unknown", require_device_guard=False):
        self.require_device_guard = require_device_guard
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.channels = tuple(channels)
        self.queue = Queue(maxsize=max(64, min(256, math.ceil(required_rate * 4 / 1024**2))))
        self.writer_metrics = dict(queue_capacity_blocks=self.queue.maxsize,
                                   max_buffer_bytes=self.queue.maxsize * 1024**2,
                                   queue_peak_blocks=0, write_calls=0,
                                   write_seconds_total=0.0, write_seconds_max=0.0,
                                   flush_fsync_seconds=0.0)
        self.metrics_lock = Lock()
        self.done = Event()
        self.error = None
        self.received = {c: 0 for c in channels}
        self.written = {c: 0 for c in channels}
        self.digests = {c: hashlib.sha256() for c in channels}
        self.files = {}
        self.report = dict(status='INCOMPLETE', info=info, channels=channels,
                           format='ci16_le', bytes_per_complex_sample=4,
                           required_bytes_per_second=required_rate,
                           absolute_continuity_proven=False,
                           continuity='unknown', simultaneous_channels_proven=False, source=source,
                           prepared_at=datetime.now(timezone.utc).isoformat(), device_sample_timestamps=False, device_guard_required=require_device_guard)
        self.report['writer_metrics'] = self.writer_metrics
        self._manifest()
        reserve = max(256 * 1024**2, required_rate * (seconds or 10))
        if shutil.disk_usage(self.directory).free < reserve:
            raise RuntimeError('Insufficient free space for recording')
        try:
            # Same-directory short write/fsync admission probe; it does not qualify sustained storage performance.
            self.report['disk_probe_bytes_per_second'] = probe_disk_rate(self.directory)
            if self.report['disk_probe_bytes_per_second'] < required_rate * 1.25:
                raise RuntimeError('Storage probe is below 125% of input rate; recording refused')
            for channel in channels:
                path = self.directory / f'rx{channel}.iq.partial'
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.files[channel] = os.fdopen(fd, 'wb', buffering=1024 * 1024)
        except Exception as error:
            for stream in self.files.values():
                stream.close()
            self.report['error'] = str(error)
            self._manifest()
            raise
        self.thread = Thread(target=self._write, name='h102-iq-writer', daemon=False)
        self.thread.start()

    def _manifest(self):
        path = self.directory / 'recording.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def submit(self, channel, raw):
        """Called by receive threads without waiting for disk; queue/write failures raise immediately and stop acquisition."""
        if self.error:
            raise RuntimeError('IQ write failed: ' + str(self.error)) from self.error
        if self.done.is_set() or channel not in self.received or not raw or len(raw) % 4 or len(raw) > 1024**2:
            raise ValueError('Recording is closed or the IQ block is invalid')
        try:
            self.queue.put_nowait((channel, raw))
        except Full as error:
            with self.metrics_lock:
                self.writer_metrics['queue_peak_blocks'] = self.queue.maxsize
            self.error = RuntimeError('IQ recording queue is full; recording is incomplete')
            raise self.error from error
        self.received[channel] += len(raw)
        with self.metrics_lock:
            self.writer_metrics['queue_peak_blocks'] = max(self.writer_metrics['queue_peak_blocks'],
                                                          self.queue.qsize())

    def _write(self):
        """Exclusively own file handles; drain after errors without fabricating successful counts."""
        try:
            while not self.done.is_set() or not self.queue.empty():
                try:
                    channel, raw = self.queue.get(timeout=.1)
                except Empty:
                    continue
                started = time.monotonic()
                size = self.files[channel].write(raw)
                elapsed = time.monotonic() - started
                self.writer_metrics['write_calls'] += 1
                self.writer_metrics['write_seconds_total'] += elapsed
                self.writer_metrics['write_seconds_max'] = max(self.writer_metrics['write_seconds_max'], elapsed)
                if size != len(raw):
                    raise OSError('Short write to IQ file')
                self.digests[channel].update(raw)
                self.written[channel] += size
            started = time.monotonic()
            for stream in self.files.values():
                stream.flush()
                os.fsync(stream.fileno())
            self.writer_metrics['flush_fsync_seconds'] = time.monotonic() - started
        except Exception as error:
            self.error = error
        finally:
            for stream in self.files.values():
                try:
                    stream.close()
                except Exception as error:
                    self.error = self.error or error

    def finish(self, session_ok, results):
        """Drain after producers exit and cross-check receive/analysis/file counts; scoped losslessness requires device windows plus byte checks."""
        self.done.set()
        self.thread.join()
        verified = bool(session_ok) and self.error is None
        by_channel = {v['channel']: v for v in results}
        records = {}
        device_verified = True
        for channel in self.channels:
            path = self.directory / f'rx{channel}.iq.partial'
            result = by_channel.get(channel, {})
            digest = self.digests[channel].hexdigest()
            ok = (self.received[channel] > 0 and self.received[channel] == self.written[channel]
                  == path.stat().st_size == result.get('bytes') and digest == result.get('stream_sha256'))
            guard=result.get('device_guard',{})
            guard_ok=bool(guard.get('passed') and guard.get('stage')=='ended'
                          and guard.get('checked_before_buffer_destroy')
                          and guard.get('initial_status')==0 and not guard.get('final_status',4)&4
                          and guard.get('cleared_ns',0)<guard.get('armed_ns',0)<guard.get('checked_ns',0))
            device_verified &= guard_ok
            verified &= ok and (guard_ok or not self.require_device_guard)
            records[str(channel)] = dict(received_bytes=self.received[channel], written_bytes=self.written[channel],
                                         samples=self.written[channel] // 4, sha256=digest, checked=ok,
                                         first_received_host_monotonic=result.get('first_received_monotonic'),
                                         last_received_host_monotonic=result.get('last_received_monotonic'),
                                         within_errors=result.get('within_errors'), boundary_errors=result.get('boundary_errors'),
                                         iq_errors=result.get('iq_errors'), timeouts=result.get('timeouts'),
                                         device_guard=guard, device_guard_checked=guard_ok)
        self.report.update(status=('PASS_ADC_DMA_TO_FILE_NO_DROP' if device_verified else 'HOST_COMPLETE_CONTINUITY_UNVERIFIED') if verified else 'INCOMPLETE',
                           absolute_continuity_proven=bool(verified and device_verified),
                           continuity=('pinned_RTL_ADC_DMA_to_file_interval' if verified and device_verified else 'unknown'),
                           host_complete=bool(verified), results=records, closed_at=datetime.now(timezone.utc).isoformat(), error=str(self.error) if self.error else None)
        if verified:
            for channel in self.channels:
                (self.directory / f'rx{channel}.iq.partial').rename(self.directory / f'rx{channel}.iq')
        self._manifest()
        if not verified:
            raise RuntimeError('Recording incomplete; raw files and recording.json retained')
        return self.report


# End of file: recorder.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
