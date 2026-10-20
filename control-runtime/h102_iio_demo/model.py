"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       model.py
@brief      Network budgets, IQ16 decoding, digital counts, display spectra and snapshot storage.
@details    Pure data processing without device access. Sum all channel demands; ordinary IQ cannot infer
            missing samples. RAMP16 checks modulo-65536 continuity only. Plot decimation preserves received originals.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.2.0
@date       2026-10-13
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

NETWORK_PROTOCOL_BUDGET = 100_000_000
"""Estimated 1 GbE protocol ceiling, not current H102 IIO sustained processing capacity."""

NETWORK_BUDGET = 64_000_000
"""Conservative H102 IIO budget in bytes/s: representative 64 MB/s passed; gaps observed at 80 MB/s."""


def mhz_to_hz(value) -> int:
    """Convert decimal MHz to integer Hz in 30-6000 MHz; reject nonfinite/sub-Hz input."""
    try:
        hz = Decimal(str(value)) * 1_000_000
    except InvalidOperation as error:
        raise ValueError("Invalid center frequency") from error
    if not hz.is_finite() or hz != hz.to_integral_value() or not 30_000_000 <= hz <= 6_000_000_000:
        raise ValueError("Center frequency must be 30-6000 MHz with 1 Hz precision")
    return int(hz)


@dataclass(frozen=True)
class NetworkBudget:
    """Freeze aggregate channel input rate; budget admission does not qualify lossless hardware operation."""

    required: int
    limit: int = NETWORK_BUDGET

    @property
    def allowed(self) -> bool:
        """Require aggregate demand of selected channels within the configured payload ceiling."""
        return 0 < self.required <= self.limit


def rates(info: dict, channels: tuple[int, ...]) -> dict[int, int]:
    """Read actual per-channel Fs from verified configuration; reject empty, duplicate or unknown selections."""
    if not channels or len(set(channels)) != len(channels) or any(c not in (1, 2) for c in channels):
        raise ValueError("Select RX1, RX2 or both RX channels")
    values = {int(v["channel"]): int(v["sample_rate_hz"]) for v in info["parameters"]["rx"]}
    if any(c not in values or values[c] <= 0 for c in channels):
        raise ValueError("Incomplete device sample-rate readback")
    return {c: values[c] for c in channels}


def budget(info: dict, channels: tuple[int, ...]) -> NetworkBudget:
    """One complex IQ sample contains two signed16 components, totaling four bytes."""
    return NetworkBudget(sum(rates(info, channels).values()) * 4)


def decode_iq(raw: bytes) -> np.ndarray:
    """Return a read-only LE signed IQ16 view; reject incomplete IQ and empty blocks."""
    if not raw or len(raw) % 4:
        raise ValueError("Invalid IQ block length or format")
    return np.frombuffer(raw, dtype="<i2").reshape(-1, 2)


class RampChecker:
    """Accumulate within/across-block and I/Q count errors per RX, retaining modulo-65536 limits."""

    def __init__(self) -> None:
        self.last: np.ndarray | None = None
        self.within = self.boundary = self.pair = 0

    def check(self, raw: bytes) -> dict[str, int]:
        """Check complete received blocks; plot windows/decimated traces do not establish continuity."""
        decode_iq(raw)
        a = np.frombuffer(raw, dtype="<u2").reshape(-1, 2).astype(np.uint32)
        inside = int(np.count_nonzero(((a[1:] - a[:-1]) & 65535) != 1))
        edge = int(np.count_nonzero(((a[0] - self.last) & 65535) != 1)) if self.last is not None else 0
        pair = int(np.count_nonzero(a[:, 0] != a[:, 1]))
        self.within += inside
        self.boundary += edge
        self.pair += pair
        self.last = a[-1].copy()
        return {"within_errors": self.within, "boundary_errors": self.boundary, "iq_errors": self.pair}


def display_frame(raw: bytes, sample_rate: int) -> dict[str, Any]:
    """Create bounded waveform and Hann amplitude spectrum; dBFS is not calibrated RF power."""
    a = decode_iq(raw)
    wave = a[: min(len(a), 2048)].astype(np.float32)
    n = min(len(a), 8192)
    n = 1 << (n.bit_length() - 1)
    v = a[:n].astype(np.float32)
    z = (v[:, 0] + 1j * v[:, 1]) / 32768
    z = z - z.mean()
    window = np.hanning(n)
    amplitude = np.abs(np.fft.fftshift(np.fft.fft(z * window))) / max(float(window.sum()), 1.0)
    spectrum = 20 * np.log10(np.maximum(amplitude, 1e-8))
    return {"time_us": np.arange(len(wave)) * 1e6 / sample_rate,
            "i": wave[:, 0], "q": wave[:, 1],
            "frequency_mhz": np.fft.fftshift(np.fft.fftfreq(n, 1 / sample_rate)) / 1e6,
            "dbfs": spectrum, "samples": len(a),
            "rms": float(np.sqrt(np.mean(v.astype(np.float64) ** 2))),
            "nonconstant": bool(np.any(np.std(v, axis=0) > 0))}


def save_json(path: Path, value: Any) -> None:
    """Create/atomically update this transaction's mutable receipt; never overwrite sealed evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def save_snapshot(directory: Path, raw_blocks: dict[int, bytes], metadata: dict) -> dict:
    """Export the latest complete IQ block and profile to a new directory; not a whole-session recording."""
    directory.mkdir(parents=True, exist_ok=False)
    records = {}
    for channel, raw in sorted(raw_blocks.items()):
        a = decode_iq(raw)
        name = f"rx{channel}.bin"
        p = directory / name
        p.write_bytes(raw)
        p.chmod(0o600)
        records[str(channel)] = {"file": name, "samples": len(a), "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(), "format": "LE interleaved signed IQ16"}
    result = {"schema": 1, "scope": "latest complete blocks; channels not phase-aligned",
              "saved_at": datetime.now(timezone.utc).isoformat(), "metadata": metadata, "channels": records}
    save_json(directory / "snapshot.json", result)
    return result

# End of file: model.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
