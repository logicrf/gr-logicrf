"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file fm_tools.py
@brief Find FM-band candidates using finite board sweeps and fully restore original profile/LO.
@details Spectral screening rejects narrow CW; it neither identifies station names nor guarantees FM demodulation.
         IQ is processed finitely on-board; verify the pinned runtime SHA and use IP-only ordinary control.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.0
@date 2026-10-17
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import numpy as np


def station_candidates(frequency_hz, dbfs, threshold_db=8.):
    """Select wideband candidates on a 100 kHz grid; reject nonfinite/uncovered regions or occupancy below 40 kHz.

    Input equal-length one-dimensional frequency-Hz/relative-dBFS arrays; return at most 32 candidates spaced 200 kHz.
    Adjacent-band median gives relative contrast, not calibrated SNR, FM identification or station names.
    """
    frequency = np.asarray(frequency_hz, dtype=float)
    level = np.asarray(dbfs, dtype=float)
    if (frequency.ndim != 1 or level.shape != frequency.shape or len(frequency) < 16
            or not np.isfinite(frequency).all() or np.any(np.diff(frequency) <= 0)
            or not np.isfinite(threshold_db) or not 3 <= threshold_db <= 30):
        raise ValueError('FM_SCAN_SPECTRUM_OR_THRESHOLD')
    spacing = float(np.median(np.diff(frequency)))
    power = 10. ** (np.clip(level, -300, 100) / 10)
    rows = []
    for center in range(88_000_000, 108_000_001, 100_000):
        delta = np.abs(frequency - center)
        band = delta <= 80_000
        nearby = (delta >= 220_000) & (delta <= 450_000)
        if (band.sum() < 8 or nearby.sum() < 8
                or not np.isfinite(level[band]).all()
                or np.isfinite(level[nearby]).sum() < nearby.sum() * .8):
            continue
        floor = float(np.nanmedian(level[nearby]))
        contrast = float(10 * np.log10(np.mean(power[band])) - floor)
        occupied = float(np.sum(level[band] >= floor + 6) * spacing)
        if contrast >= threshold_db and occupied >= 40_000:
            rows.append(dict(frequency_hz=center, contrast_db=contrast,
                             power_dbfs=float(10 * np.log10(np.mean(power[band]))),
                             occupied_hz=occupied, qualification='spectral_candidate'))
    distinct = []
    for row in sorted(rows, key=lambda value: value['power_dbfs'], reverse=True):
        if all(abs(row['frequency_hz'] - old['frequency_hz']) >= 200_000 for old in distinct):
            distinct.append(row)
        if len(distinct) == 32:
            break
    return sorted(distinct, key=lambda value: value['frequency_hz'])


def scan_stations(host, channel, output, cancel, progress, threshold_db=8.):
    """Run one finite Ethernet sweep in a worker; cancel is an Event; progress emits ordinary dictionaries only.

    Call only with this application stopped and device idle. Attempt restoration on errors; raise if recovery is unconfirmed.
    Never modify board services or authorize RF TX. Return native results, elapsed scan time and full recovery summary.
    """
    from gnuradio import logicrf
    from gnuradio.logicrf.managed import load_runtime
    from h102_control.client import Client, Connection
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    connection = Connection(host)
    client = Client(connection)
    baseline = client.status()
    if not baseline['actual']['data_idle']:
        raise RuntimeError('FM_SCAN_REQUIRES_IDLE_DEVICE')
    dummy = logicrf.make_duplex_device(backend='iio', uri='ip:' + host,
        configuration='4msps-2mhz', sample_rate=4_000_000,
        rx_channels=(channel,), tx_channels=(), rf_enabled=False)
    backend = load_runtime(dummy.controller.runtime, dummy.controller.lock_path)
    from h102_iio_demo.sweep import SweepPlan, SweepSession

    class ReportingSweep(SweepSession):
        """Emit immutable progress after each completed segment; SweepSession retains sole hardware ownership."""
        def _publish(self, accumulator, segments, complete=False):
            super()._publish(accumulator, segments, complete)
            progress(dict(stage='scanning', completed=len(segments),
                          total=len(self.plan.centers())))

    record = dict(status='RUNNING', RF_TX=False, schema=1,
                  started_at=datetime.now(timezone.utc).isoformat(),
                  original_profile=baseline['info']['active'])
    started = time.monotonic()
    try:
        progress(dict(stage='preparing', completed=0, total=1))
        if cancel.is_set():
            record['status'] = 'CANCELLED'
        else:
            group = next(g for g in baseline['info']['radio']['groups'] if channel in g['channels'])
            state = baseline if group['editable'] and not group['tx_shared'] else backend.inspect(connection, '4msps-2mhz')
            actual = next(row for row in state['info']['parameters']['rx'] if row['channel'] == channel)
            fs, bandwidth = actual['sample_rate_hz'], actual['rf_bandwidth_hz']
            if fs < 1_000_000 or bandwidth < 400_000:
                state = backend.inspect(connection, '4msps-2mhz')
                fs, bandwidth = 4_000_000, 2_000_000
            record['scan_profile'] = state['info']['active']
            record['preparation_seconds'] = time.monotonic() - started
            plan = SweepPlan(87_800_000, 108_200_000, fs, bandwidth,
                             fft_points=8192, averages=4, settle_ms=2)
            session = ReportingSweep(connection, state['info'], channel, plan, output / 'native')
            session.stopping = cancel
            native = session.run()
            # The fixed compatibility adapter replaces legacy SSH with typed APIs; do not inherit old transport descriptions.
            native['execution'] = 'board local IQ and FFT; bounded frequency cells over fixed H102 API'
            record['native'] = native
            if native['status'] not in ('PASS_SWEEP_SESSION', 'STOPPED_SWEEP_SESSION') or not native.get('recovery_confirmed'):
                raise RuntimeError('FM_SCAN_NATIVE_FAILED:' + native['status'])
            frame = session.completed_frame
            if cancel.is_set() or frame is None:
                record['status'] = 'CANCELLED'
            else:
                candidates = station_candidates(frame['frequency_mhz'] * 1e6, frame['dbfs'], threshold_db)
                record.update(status='PASS_SPECTRAL_SCAN', candidates=candidates,
                              qualification='spectral_candidates_only; FM/RDS identity not established')
    except Exception as error:
        record.update(status='FAIL', error=repr(error))
    finally:
        progress(dict(stage='restoring', completed=0, total=1))
        try:
            current = client.status()
            if current['info']['active'] != baseline['info']['active']:
                current = client.request('profile.select', dict(profile=baseline['info']['active'],
                    boot_id=baseline['info']['boot_id']), timeout=240)
            for group in baseline['info']['radio']['groups']:
                hz = baseline['info']['radio']['rx_center_frequency_hz'][str(group['channels'][0])]
                if any(current['info']['radio']['rx_center_frequency_hz'][str(c)] != hz for c in group['channels']):
                    current = backend.tune_rx_idle(connection, current, group['channels'], hz,
                                                   output / ('restore-lo-' + str(group['lo'])))
            final = client.status()
            record['baseline_restored'] = all(final['actual'][key] == baseline['actual'][key]
                for key in ('identity', 'parameters', 'radio', 'data_idle', 'kernel'))
            if not record['baseline_restored']:
                raise RuntimeError('FM_SCAN_BASELINE_NOT_RESTORED')
        except Exception as error:
            record.update(status='FAIL', baseline_restored=False, recovery_error=repr(error))
        record['elapsed_seconds'] = time.monotonic() - started
        (output / 'scan.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    if record['status'] == 'FAIL':
        raise RuntimeError(str(record.get('error') or record.get('recovery_error')) + '; report: ' + str(output / 'scan.json'))
    return record
# End of file: fm_tools.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
