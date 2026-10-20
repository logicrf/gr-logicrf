"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_fm_candidates.py
@brief Check FM candidate boundaries for wide spectra, narrow CW, noise and missing coverage, without station identification.
@details Finite synthetic spectra only; exclude narrow interferers/uncovered bands without device access.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.0
@date 2026-10-17
"""
from pathlib import Path
import sys
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from fm_tools import station_candidates


class Candidates(unittest.TestCase):
    """Use fixed synthetic inputs covering false-positive and missed-station boundaries."""
    def test_broad_stations_reject_narrow_cw(self):
        frequency = np.arange(87_500_000, 108_500_001, 500.)
        power = np.ones(len(frequency)) * 1e-10
        for center in (88_000_000, 96_500_000, 108_000_000):
            power += 1e-7 * np.exp(-((frequency - center) / 40000)**2)
        power += 1e-5 * np.exp(-((frequency - 101_100_000) / 1000)**2)
        rows = station_candidates(frequency, 10 * np.log10(power))
        self.assertEqual([row['frequency_hz'] for row in rows], [88_000_000, 96_500_000, 108_000_000])
        self.assertTrue(all(row['qualification'] == 'spectral_candidate' for row in rows))

    def test_noise_and_missing_coverage_do_not_create_stations(self):
        frequency = np.arange(87_500_000, 108_500_001, 500.)
        power = np.random.default_rng(3).exponential(1e-10, len(frequency))
        level = 10 * np.log10(power)
        level[(frequency > 96_400_000) & (frequency < 96_600_000)] = np.nan
        self.assertEqual(station_candidates(frequency, level), [])

    def test_invalid_grid_and_threshold(self):
        for frequency, level, threshold in ((np.ones(16), np.zeros(16), 8), (np.arange(16), np.zeros(15), 8), (np.arange(16), np.zeros(16), float('nan'))):
            with self.assertRaises(ValueError): station_candidates(frequency, level, threshold)


if __name__ == '__main__': unittest.main()
# End of file: qa_fm_candidates.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
