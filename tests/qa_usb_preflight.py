"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_preflight.py
@brief Verify USB profile-switch identity checks and no retry on uncertain results, without hardware.
@details Fixed API mocks establish control contracts only, not USB throughput/GNSS/hardware recovery.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.2
@date 2026-10-17
"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from gnuradio.logicrf.usb_managed import UsbController


class UsbPreflight(unittest.TestCase):
    """Independently check pre-change/post-receipt rejection and exactly-once modification."""
    def setUp(self):
        self.controller = UsbController.__new__(UsbController)
        self.controller.connection = object()
        self.controller.machine_id = 'machine'
        self.controller.profile = 'new'
        self.pins = dict(boot_pins={'boot':'sha'}, kernel='kernel', metadata_sha256='metadata')
        self.before = dict(actual=dict(identity=dict(boot_pins={'boot':'sha'}, machine_id='machine',
                             boot_id='boot'), kernel='kernel', metadata_sha256='metadata'),
                           info=dict(active='old', api='68.21.6'))
        self.after = deepcopy(self.before)
        self.after['info']['active'] = 'new'
        self.backend = SimpleNamespace(inspect=Mock(return_value=self.before))

    def inspect(self, reply=None, error=None):
        """Inject fixed API receipts and retain exactly one call; every network entry is a substitute."""
        with patch('h102_control.client.Client') as client:
            request = client.return_value.request
            request.return_value = self.after if reply is None else reply
            request.side_effect = error
            try:
                return self.controller._inspect_profile(self.backend, self.pins)
            finally:
                self.request = request

    def test_profile_select_uses_verified_boot_without_second_status(self):
        self.assertEqual(self.inspect(), self.after)
        self.backend.inspect.assert_called_once_with(self.controller.connection)
        self.request.assert_called_once_with('profile.select', dict(profile='new', boot_id='boot'), timeout=240)

    def test_unchanged_profile_is_read_only(self):
        self.controller.profile = 'old'
        self.assertEqual(self.inspect(), self.before)
        self.request.assert_not_called()

    def test_wrong_initial_identity_never_changes_configuration(self):
        self.before['actual']['identity']['machine_id'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'USB_BOARD_IDENTITY'):
            self.inspect()
        self.request.assert_not_called()

    def test_changed_receipt_never_becomes_ready(self):
        for part, key, value, error in (
                ('identity', 'boot_id', 'other', 'USB_PROFILE_CHANGED_IDENTITY'),
                ('actual', 'kernel', 'other', 'USB_PROFILE_CHANGED_FIRMWARE'),
                ('actual', 'metadata_sha256', 'other', 'USB_PROFILE_CHANGED_FIRMWARE'),
                ('info', 'active', 'old', 'USB_PROFILE_READBACK'),
                ('info', 'api', 'other', 'CONTROL_API_REQUIRES')):
            with self.subTest(part=part, key=key):
                reply = deepcopy(self.after)
                target = reply['actual']['identity'] if part=='identity' else reply[part]
                target[key] = value
                with self.assertRaisesRegex(RuntimeError, error):
                    self.inspect(reply=reply)
                self.request.assert_called_once()

    def test_unknown_mutation_result_is_not_retried(self):
        with self.assertRaises(TimeoutError):
            self.inspect(error=TimeoutError('unknown'))
        self.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()

# End of file: qa_usb_preflight.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
