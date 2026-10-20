"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_rx_batch_managed.py
@brief Combine dual-RX ADC begin/arm requests after actual buffer creation and warmup.
@details Inherit generation/end/archive workflows. Native threads meet at barriers; the last participant sends
         one request and verifies both proofs. Missing/duplicate/timed-out/error participants fail; never reuse prior-generation proofs.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from threading import Condition

from .iio_rx_managed import IioRxController


class IioBatchRxController(IioRxController):
    """Keep the single-channel path; dual native RX threads rendezvous only on guard calls.

    The lifecycle thread resets barriers while data threads are inactive. Do not hold command_lock while waiting,
    so the peer and health renewals can progress. Enter once per channel/stage/generation.
    """
    entrypoint = 'board_rx_batch_stream.py'

    def __init__(self, *args, **kwargs):
        self.batch_condition = Condition()
        self.batch_states = {}
        super().__init__(*args, **kwargs)

    def _reset_end(self):
        """Reset three stages while data threads are stopped; preserve parent end-barrier semantics."""
        super()._reset_end()
        with self.batch_condition:
            self.batch_states = {}

    def guard(self, channel, stage):
        """Run grouped begin/arm only after both buffers exist and each drains 34 blocks.

        Wait at most eight seconds for the peer; wake all on failure and expose it to late arrivals.
        Never send a delayed board request after timeout; persist actual per-channel proofs.
        """
        if stage not in ('begin', 'arm') or len(self.rx_channels) == 1:
            return super().guard(channel, stage)
        with self.batch_condition:
            state = self.batch_states.setdefault(stage, dict(pending=set(), proofs=None, error=None))
            if channel not in self.rx_channels or channel in state['pending']:
                state['error'] = 'RX_BATCH_BARRIER_CHANNEL'
                self.batch_condition.notify_all()
            if state['error']:
                raise RuntimeError(state['error'])
            state['pending'].add(channel)
            if state['pending'] != set(self.rx_channels):
                if not self.batch_condition.wait_for(lambda: state['proofs'] is not None or state['error'], 8):
                    state['error'] = 'RX_BATCH_BARRIER_TIMEOUT'
                    self.batch_condition.notify_all()
                if state['error']:
                    raise RuntimeError(state['error'])
                return
        try:
            with self.command_lock:
                if not self.link or not self.link.permitted():
                    raise RuntimeError('STREAM_OWNER_PERMISSION_EXPIRED')
                proofs = self.link._command(dict(action='guard_'+stage+'_all', channel=0))['guard']
                expected = 'warming' if stage == 'begin' else 'armed'
                if set(proofs) != {str(ch) for ch in self.rx_channels} or any(
                        row.get('stage') != expected or row.get('channel') != int(ch)
                        for ch, row in proofs.items()):
                    raise RuntimeError('RX_BATCH_'+stage.upper()+'_READBACK')
                for ch, proof in proofs.items():
                    self.guard_records.setdefault('rx'+ch, {})[stage] = proof
                self._save('guards.json', self.guard_records)
            with self.batch_condition:
                if state['error']:
                    raise RuntimeError(state['error'])
                state['proofs'] = proofs
                self.batch_condition.notify_all()
        except Exception as error:
            with self.batch_condition:
                state['error'] = str(error)
                self.batch_condition.notify_all()
            raise

# End of file: iio_rx_batch_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
