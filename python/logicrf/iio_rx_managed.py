"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_rx_managed.py
@brief Manage cross-generation GRC single/dual RX pause/tuning and final verified archival.
@details Inherit accepted IIO preparation, ADC windows and independent post; receive-only with RF TX off.
         Persist every generation guard and keep latest 32 in RAM; archive owned directories after successful recovery only.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from copy import deepcopy
import math
from threading import Condition
import time

from .fm_managed import FmController


class IioRxController(FmController):
    """Control thread alone prepares/pauses/tunes/closes; monitor renews liveness only.

    fm limits belong to the user entry; the new board entry independently validates full-band RX without shared TX LO.
    Recovery failures preserve board originals; archival failure never erases completed hardware recovery evidence.
    """
    entrypoint = 'board_rx_stream.py'
    parallel_idle_warmup = True

    def __init__(self, *args, fm=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.frequency_range = (88_000_000, 108_000_000) if fm else (50_000_000, 5_800_000_000)
        self.fm = fm
        self.epoch_count = 0
        self.end_condition = Condition()
        self.end_pending = set()
        self.end_proofs = None
        self.end_error = None

    def _reset_end(self):
        """Clear current barriers before data threads start; never reuse prior ADC proofs."""
        with self.end_condition:
            self.end_pending = set()
            self.end_proofs = None
            self.end_error = None

    def prepare_direction(self, direction, mask):
        """Reset end barriers at initial preparation; retune resets paused generations."""
        with self.lifecycle:
            if self.prepared_options is None:
                self._reset_end()
            return super().prepare_direction(direction, mask)

    def guard(self, channel, stage):
        """After both final refills, check both windows once before permitting buffer destruction.

        The first waiter accepts no additional IQ; board end follows both channels' final data and precedes destruction.
        A missing participant/failed check fails; never reuse one channel's proof for its peer.
        """
        if stage != 'end' or len(self.rx_channels) == 1:
            return super().guard(channel, stage)
        with self.end_condition:
            if channel not in self.rx_channels or channel in self.end_pending:
                raise RuntimeError('RX_END_BARRIER_CHANNEL')
            self.end_pending.add(channel)
            leader = self.end_pending == set(self.rx_channels)
            if not leader:
                if not self.end_condition.wait_for(lambda: self.end_proofs is not None or self.end_error, 8):
                    self.end_error = 'RX_END_BARRIER_TIMEOUT'
                    self.end_condition.notify_all()
                if self.end_error:
                    raise RuntimeError(self.end_error)
                return
        try:
            with self.command_lock:
                if not self.link or not self.link.permitted():
                    raise RuntimeError('STREAM_OWNER_PERMISSION_EXPIRED')
                proofs = self.link._command(dict(action='guard_end_all', channel=0))['guard']
                if set(proofs) != {str(ch) for ch in self.rx_channels} or any(
                        not row.get('passed') or row.get('channel') != int(ch) for ch, row in proofs.items()):
                    raise RuntimeError('RX_END_ALL_READBACK')
                for ch, proof in proofs.items():
                    self.guard_records.setdefault('rx'+ch, {})['end'] = proof
                self._save('guards.json', self.guard_records)
            with self.end_condition:
                self.end_proofs = proofs
                self.end_condition.notify_all()
        except Exception as error:
            with self.end_condition:
                self.end_error = str(error)
                self.end_condition.notify_all()
            raise

    def finish_direction(self, direction):
        """Seal generation guards after normal drain; retain ownership only when every declared RX ended."""
        with self.lifecycle:
            if self.hold_for_tune and not self.rx_ref().error():
                if (direction != 'rx' or self.tx_channels or self.rf_enabled or
                        set(self.guard_records) != {'rx'+str(ch) for ch in self.rx_channels} or
                        any(not row.get('end', {}).get('passed') for row in self.guard_records.values())):
                    raise RuntimeError('RX_EPOCH_GUARD_NOT_CLOSED')
                self._save('epoch-%08d.json' % self.epoch_count, deepcopy(self.guard_records))
                self.epochs.append(deepcopy(self.guard_records))
                del self.epochs[:-32]
                self.epoch_count += 1
                self.paused = True
                return
            super().finish_direction(direction)

    def retune(self, frequency):
        """Validate entry frequency range first; preserve strict native-readback/next-buffer ordering."""
        # C++ frequency options use double; same-frequency restoration must accept exact integer-Hz readback.
        if (isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or
                not math.isfinite(frequency) or frequency != int(frequency) or
                not self.frequency_range[0] <= frequency <= self.frequency_range[1]):
            raise ValueError('FM_FREQUENCY_RANGE' if self.fm else 'RX_FREQUENCY_RANGE')
        if getattr(self, 'info', None) and all(self.info['radio']['rx_center_frequency_hz'][str(ch)] == frequency
                             for ch in self.rx_channels):
            with self.lifecycle, self.command_lock:
                self.check_cancelled()
                if not self.paused or not self.link or not self.link.permitted():
                    raise RuntimeError('RX_RESUME_REQUIRES_PAUSED_OWNER')
                started = time.monotonic()
                proof = self.link._command(dict(action='guard_reset', channel=0))['guard']
                if proof.get('passed') is not True or proof.get('current_settings_verified') is not True:
                    raise RuntimeError('RX_RESUME_SETTINGS_NOT_VERIFIED')
                self.rx_ref().set_center_frequency(int(frequency))
                # Board reread confirmed every setting equals this owner's active state; same-frequency resume needs no LO remap.
                self.guard_records = {}
                self.finished_directions.clear()
                self.hold_for_tune = False
                self._reset_end()
                self._save('retune.json', dict(frequency_hz=int(frequency), seconds=time.monotonic()-started,
                    owner=self.link.ready, radio=self.info['radio'], epoch=self.epoch_count,
                    same_frequency_settings_verified=True))
                return dict(radio=self.info['radio'])
        result = super().retune(int(frequency))
        self._reset_end()
        return result

    def finish(self):
        """After successful independent post, download/restore-verify/retire this run directory; preserve originals on failure."""
        link, connection = self.link, self.connection
        super().finish()
        if link is not None and self.recovery_confirmed:
            from .iio_runtime_archive import retire
            retire(self, link, connection)

# End of file: iio_rx_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
