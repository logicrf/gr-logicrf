"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file fm_scan_managed.py
@brief Sweep inside a paused single-RX FM session, close local DMA and restore listening settings.
@details Handle finite spectral bins only, without IQ transport. A lifecycle lock covers scanning/final release.
         New data generations retain 32 buffers/34-block drain/ADC guard; propagate recovery failures unchanged.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import time

import numpy as np

from .iio_rx_batch_managed import IioBatchRxController


class FmScanController(IioBatchRxController):
    """One scanner borrows paused ownership; close waits on the same lifecycle lock and never races DMA."""
    entrypoint = 'board_fm_scan_stream.py'

    def supports_fast_scan(self):
        """Determine eligibility from actual readback only; other valid FM profiles retain cold scanning."""
        info = getattr(self,'info',None) or {}
        radio = info.get('radio',{})
        if len(self.rx_channels)!=1 or radio.get('initial_calibrations')!='off':return False
        ch = self.rx_channels[0]
        actual = next((p for p in info.get('parameters',{}).get('rx',[]) if p['channel']==ch),{})
        group = next((g for g in radio.get('groups',[]) if ch in g['channels']),{})
        return ((actual.get('sample_rate_hz'),actual.get('rf_bandwidth_hz'))==(1536000,500000) and
                group.get('editable') is True and not group.get('tx_shared'))

    def scan_stations(self, output, cancel, progress):
        """Bound FM grid/candidates; cancellation still restores; recovery failure prevents further reception."""
        from .fm_tools import station_candidates
        from h102_iio_demo.sweep import SweepPlan, Accumulator
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        record = dict(status='RUNNING', RF_TX=False, same_owner=True, source=self.entrypoint,
                      owner=deepcopy(self.link.ready) if self.link else None)
        opened = False
        with self.lifecycle, self.command_lock:
            try:
                if (not self.paused or len(self.rx_channels)!=1 or self.rf_enabled or self.tx_channels or
                        not self.link or not self.link.permitted()):
                    raise RuntimeError('FM_SCAN_REQUIRES_PAUSED_SINGLE_RX')
                if self.info['radio'].get('initial_calibrations')!='off':
                    raise RuntimeError('FM_FAST_SCAN_REQUIRES_OFF_CALIBRATION')
                plan = SweepPlan(87800000,108200000,1536000,500000,fft_points=8192,averages=4,settle_ms=2)
                native = dict(vars(plan), grid_size=len(plan.grid()))
                accumulator = Accumulator(plan)
                proof = self.link._command(dict(action='sweep_setup',channel=0,plan=native,centers=[]))['guard']
                if proof.get('setup') is not True:raise RuntimeError('FM_SCAN_SETUP_NOT_PROVED')
                opened = True
                record['setup_seconds'] = time.monotonic()-started
                centers = plan.centers()
                segments = []
                for first in range(0,len(centers),14):
                    if cancel.is_set():break
                    batch = centers[first:first+14]
                    reply = self.link._command(dict(action='sweep_batch',channel=0,plan=native,centers=batch))['guard']
                    rows = reply['segments']
                    if [row['center_hz'] for row in rows]!=batch or reply.get('stopped') is not False:
                        raise RuntimeError('FM_SCAN_BATCH_COVERAGE')
                    for row in rows:
                        raw = base64.b64decode(row.pop('power_f32le_b64'),validate=True)
                        if len(raw)%4:raise RuntimeError('FM_SCAN_CELL_LENGTH')
                        accumulator.add_cells(row['grid_first'],np.frombuffer(raw,dtype='<f4'))
                        segments.append(row)
                    progress(dict(stage='scanning',completed=len(segments),total=len(centers)))
                record.update(segments=segments,scan_seconds=time.monotonic()-started)
                if cancel.is_set():
                    record['status']='CANCELLED'
                else:
                    spectrum = accumulator.dbfs()
                    if len(segments)!=len(centers) or not np.isfinite(spectrum).all():
                        raise RuntimeError('FM_SCAN_INCOMPLETE_GRID')
                    np.savez_compressed(output/'spectrum.npz',frequency_hz=accumulator.frequency,dbfs=spectrum)
                    record.update(status='PASS_SPECTRAL_SCAN',candidates=station_candidates(accumulator.frequency,spectrum),
                                  qualification='spectral_candidates_only; not continuous IQ or FM identity')
            except Exception as error:
                record.update(status='FAIL',error=repr(error))
            finally:
                if opened:
                    try:
                        proof = self.link._command(dict(action='sweep_finish',channel=0))['guard']
                        if (proof.get('scan_restored') is not True or proof.get('local_dma_closed') is not True or
                                proof.get('radio')!=self.info['radio']):
                            raise RuntimeError('FM_SCAN_RESTORE_NOT_PROVED')
                        record.update(scan_settings_restored=True,restore=proof)
                    except Exception as error:
                        record.update(status='FAIL',scan_settings_restored=False,recovery_error=repr(error))
                record['elapsed_seconds'] = time.monotonic()-started
                (output/'scan.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
                if record['status']=='FAIL':self.rx_ref().invalidate('FM_SCAN:'+str(record.get('recovery_error') or record.get('error')))
        if record['status']=='FAIL':raise RuntimeError(record.get('recovery_error') or record.get('error'))
        return record

    def finish(self):
        """Window close waits for finite scanning/recovery before closing its control channel."""
        with self.lifecycle:
            return super().finish()

# End of file: fm_scan_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
