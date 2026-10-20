"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file fm_managed.py
@brief Retain managed single-RX FM IIO sessions across tuning generations.
@details Native Sources handle all data; end complete ADC windows and destroy buffers before each actor retune.
         Final stop/errors still run independent post; this path does not provide seamless IQ recording.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.1
@date 2026-10-17
"""
from copy import deepcopy
import json
import time

from .iio_stream_managed import DuplexDevice, IioStreamController


class FmController(IioStreamController):
    """Single RX/RF off only; the control thread serializes prepare, pause, tune and final close."""
    entrypoint = 'board_fm_stream.py'

    def __init__(self, *args, stop_event, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_event = stop_event
        self.hold_for_tune = False
        self.paused = False
        self.epochs = []
        self.preparing = False
        self.cancelled_preparation = False
        self.session_link = None

    def check_cancelled(self):
        """Cancel between bounded hardware preparation steps; never claim unstarted windows passed."""
        if self.stop_event.is_set():
            raise InterruptedError('FM_PREPARE_CANCELLED')

    def configure_link(self, link):
        """Forward stop through native preparation on cancellation; independent post remains responsible for lock release."""
        link.cancel_event = self
        self.session_link = link

    def requires_recovery(self):
        """Retain original link-start evidence; clearing a current link cannot erase failed-post recovery duties."""
        return self.session_link is not None and self.session_link.active

    def is_set(self):
        """Forward cancellation only while preparing; active reception ends ADC windows before normal ownership release."""
        cancelled = self.preparing and self.stop_event.is_set()
        if cancelled: self.cancelled_preparation = True
        return cancelled

    def prepare_direction(self, direction, mask):
        self.check_cancelled()
        self.paused = False
        self.preparing = True
        try:
            return super().prepare_direction(direction, mask)
        finally:
            self.preparing = False

    def finish_direction(self, direction):
        """Retain ownership only for explicit station changes; save generation guard and never equate pause with full recovery."""
        with self.lifecycle:
            if self.hold_for_tune and not self.rx_ref().error():
                if any('arm' in row and not row.get('end', {}).get('passed')
                       for row in self.guard_records.values()):
                    raise RuntimeError('FM_EPOCH_GUARD_NOT_CLOSED')
                self.epochs.append(deepcopy(self.guard_records))
                self._save('epochs.json', self.epochs)
                self.paused = True
                return
            super().finish_direction(direction)

    def retune(self, frequency):
        """Retune shared RX LO after destroying all buffers; verify driver readback without restoring the original ownership snapshot."""
        with self.lifecycle, self.command_lock:
            self.check_cancelled()
            if not self.paused or self.tx_channels or not self.link or not self.link.permitted():
                raise RuntimeError('FM_RETUNE_REQUIRES_PAUSED_RX_OWNER')
            started = time.monotonic()
            reply = self.link.tune(int(frequency), list(self.rx_channels))
            radio = reply['radio']
            if any(radio['rx_center_frequency_hz'][str(ch)] != frequency for ch in self.rx_channels):
                raise RuntimeError('FM_RETUNE_READBACK_MISMATCH')
            self.info = deepcopy(self.info)
            self.info['radio'] = radio
            self.link.info = self.info
            native = self.rx_ref()
            native.set_center_frequency(frequency)
            self.guard_records = {}
            self.finished_directions.clear()
            self.hold_for_tune = False
            self._save('retune.json', dict(frequency_hz=frequency, seconds=time.monotonic()-started,
                                          owner=self.link.ready, radio=radio,
                                          epoch=getattr(self, 'epoch_count', len(self.epochs))))
            return reply


class FmDevice(DuplexDevice):
    """FM pause reuse; final close must leave neither health threads nor board locks."""
    def close(self):
        self.controller.hold_for_tune = False
        super().close()


def make_fm_device(*, stop_event, channel, **kwargs):
    """Construct hardware-free single RX with 65536-sample blocks; missing service entry produces an explicit upgrade requirement."""
    from . import make_duplex_device
    board = make_duplex_device(backend='iio', rx_channels=(channel,), tx_channels=(),
                               rf_enabled=False, **kwargs)
    old = board.controller
    controller = FmController(board.rx_native, old.host, old.username, old.known_hosts,
        old.runtime, old.lock_path, old.firmware_lock, old.evidence, old.profile,
        old.provider, old.machine_id, old.pattern, rx_native=board.rx_native,
        rx_channels=(channel,), tx_channels=(), rf_enabled=False,
        rx_gain_db={channel:20}, stop_event=stop_event)
    board.rx_native.set_frame_samples(65536)
    return FmDevice(None, board.rx_native, controller)

# End of file: fm_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
