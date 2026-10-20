#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_iio_runtime_archive.py
@brief Verify strict IIO generation control, dual-end barriers and archive recovery boundaries.
@details Offline native devices/file fixtures only; cover corruption, traversal,
         missing channels, fresh same-frequency readback and full generation identity beyond bounded caches.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import ast
import math
import hashlib
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

from gnuradio.logicrf.iio_runtime_archive import restore_verified

ROOT = '/run/h102-gui-session-'+'a'*32
DATA = b'independent recovery evidence\n'


def packed(*, change=None, extra=None):
    """Create small independent evidence and inject boundary corruption without using production archive generation."""
    manifest = {ROOT: dict(kind='directory'), ROOT+'/post.json':
        dict(kind='file', bytes=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())}
    if change:
        change(manifest)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        item = tarfile.TarInfo('manifest.json')
        data = json.dumps(manifest).encode();item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
        item = tarfile.TarInfo(ROOT.lstrip('/'));item.type = tarfile.DIRTYPE
        archive.addfile(item)
        item = tarfile.TarInfo(ROOT.lstrip('/')+'/post.json');item.size = len(DATA)
        archive.addfile(item, io.BytesIO(DATA))
        if extra:
            archive.addfile(extra)
    return buffer.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_dual_rx_end_waits_for_both_and_checks_both_once(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        controller = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='2msps-0p25mhz', sample_rate=2000000).controller
        controller._save = Mock()
        controller.link = Mock()
        controller.link._command.return_value = {'guard':
            {str(ch): dict(channel=ch, passed=True) for ch in (1, 2)}}
        entered = Event()

        def first():
            entered.set()
            controller.guard(1, 'end')

        with ThreadPoolExecutor(2) as workers:
            one = workers.submit(first)
            self.assertTrue(entered.wait(1))
            self.assertFalse(one.done())
            controller.link._command.assert_not_called()
            two = workers.submit(controller.guard, 2, 'end')
            one.result(2);two.result(2)
        controller.link._command.assert_called_once_with(dict(action='guard_end_all', channel=0))
        self.assertEqual(set(controller.guard_records), {'rx1', 'rx2'})
        controller._reset_end()
        controller.link._command.side_effect = RuntimeError('ADC_OVERFLOW')
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(controller.guard, ch, 'end') for ch in (1, 2)]
            for future in futures:
                with self.assertRaisesRegex(RuntimeError, 'ADC_OVERFLOW'):
                    future.result(2)

    def test_begin_arm_batch_waits_for_both_and_propagates_bad_proofs(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        controller = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='2msps-0p25mhz', sample_rate=2000000).controller
        controller._save = Mock();controller.link = Mock()
        for stage, expected in (('begin', 'warming'), ('arm', 'armed')):
            controller.link._command.reset_mock()
            controller.link._command.return_value = {'guard':
                {str(ch): dict(channel=ch, stage=expected) for ch in (1, 2)}}
            entered = Event()
            def first():
                entered.set();controller.guard(1, stage)
            with ThreadPoolExecutor(2) as workers:
                one = workers.submit(first)
                self.assertTrue(entered.wait(1));self.assertFalse(one.done())
                controller.link._command.assert_not_called()
                two = workers.submit(controller.guard, 2, stage)
                one.result(2);two.result(2)
            controller.link._command.assert_called_once_with(dict(action='guard_'+stage+'_all', channel=0))
            self.assertTrue(all(controller.guard_records['rx'+str(ch)][stage]['stage']==expected for ch in (1,2)))
        controller._reset_end()
        controller.link._command.return_value = {'guard': {'1': dict(channel=1, stage='warming')}}
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(controller.guard, ch, 'begin') for ch in (1,2)]
            for future in futures:
                with self.assertRaisesRegex(RuntimeError, 'RX_BATCH_BEGIN_READBACK'):
                    future.result(2)

    def test_bounded_epoch_cache_keeps_absolute_evidence_identity(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='2msps-0p25mhz', sample_rate=2000000)
        controller = board.controller
        controller.epochs = [{}]*32
        controller.epoch_count = 40
        controller.paused = True
        controller.info = {'radio': {'rx_center_frequency_hz': {'1': 1000000000, '2': 1000000000}}}
        controller.link = Mock()
        controller.link.tune.return_value = {'radio':
            {'rx_center_frequency_hz': {'1': 2400000000, '2': 2400000000}}}
        controller._save = Mock()
        controller.retune(2400000000)
        self.assertEqual(controller._save.call_args.args[1]['epoch'], 40)
        self.assertEqual(len(controller.epochs), 32)

    def test_same_frequency_resume_revalidates_settings_and_rejects_bad_proof(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='2msps-0p25mhz', sample_rate=2000000)
        controller = board.controller
        controller.paused = True
        controller.info = {'radio': {'rx_center_frequency_hz': {'1': 2400000000, '2': 2400000000}}}
        controller.link = Mock();controller._save = Mock()
        controller.link._command.return_value = {'guard': {'passed': True, 'current_settings_verified': True}}
        controller.retune(2400000000.0)
        controller.link._command.assert_called_once_with(dict(action='guard_reset', channel=0))
        self.assertTrue(controller._save.call_args.args[1]['same_frequency_settings_verified'])
        controller.link._command.return_value = {'guard': {'passed': True}}
        with self.assertRaisesRegex(RuntimeError, 'RX_RESUME_SETTINGS_NOT_VERIFIED'):
            controller.retune(2400000000.0)

    def test_resume_accepts_native_double_integer_hz(self):
        from gnuradio.logicrf.grc_session import make_grc_rx_device
        board = make_grc_rx_device(host='192.0.2.1', channels=(1, 2),
            profile='2msps-0p25mhz', sample_rate=2000000)
        with patch('gnuradio.logicrf.fm_managed.FmController.retune', return_value={}) as tune:
            board.controller.retune(2400000000.0)
            tune.assert_called_once_with(2400000000)
            for value in (True, float('nan'), 2400000000.5, 49000000):
                with self.assertRaises(ValueError):board.controller.retune(value)

    def test_rx_actor_rejects_tx_and_invalid_plan_before_hardware(self):
        path = Path(__file__).resolve().parents[1]/'python/logicrf/board_rx_stream.py'
        function = next(n for n in ast.parse(path.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == 'validate')
        namespace = {'math': math}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        base = dict(channels=[], samples=0, rate=2000000, frequency=2400000000,
                    rx_channels=[1, 2], rx_frequency=2400000000, rf_enabled=False)
        self.assertEqual(namespace['validate'](base), 0)
        for change in (dict(channels=[1]), dict(rf_enabled=True), dict(rx_channels=[]),
                       dict(rx_channels=[1, 1]), dict(rx_frequency=49000000), dict(rate=20000000)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                namespace['validate'](dict(base, **change))

    def test_real_restore_exact_tree(self):
        with TemporaryDirectory() as temp:
            output = Path(temp)/'restored'
            self.assertEqual(restore_verified(packed(), output, ROOT),
                dict(files=1, entries=2, restore_verified=True))
            self.assertEqual((output/ROOT.lstrip('/')/'post.json').read_bytes(), DATA)

    def test_corruption_and_unlisted_or_unsafe_members_are_rejected(self):
        bad_sha = packed(change=lambda m: m[ROOT+'/post.json'].update(sha256='0'*64))
        symlink = tarfile.TarInfo(ROOT.lstrip('/')+'/outside')
        symlink.type = tarfile.SYMTYPE;symlink.linkname = '/etc/passwd'
        for raw in (bad_sha, packed(extra=tarfile.TarInfo('../escape')),
                    packed(extra=tarfile.TarInfo('unlisted')), packed(extra=symlink),
                    packed(extra=tarfile.TarInfo(ROOT.lstrip('/')+'/post.json'))):
            with self.subTest(raw=len(raw)), TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    restore_verified(raw, Path(temp)/'restored', ROOT)
                self.assertFalse((Path(temp)/'escape').exists())


if __name__ == '__main__':
    unittest.main()

# End of file: qa_iio_runtime_archive.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
