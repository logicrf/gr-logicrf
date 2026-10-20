"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_stream_managed.py
@brief Manage independent runtimes for arbitrary IQ and continuous USB TX.
@details Retain both native main/posts and SSI signatures, separate from RAMP32 testing; short permits are unchanged.
         Verify each board SDK source SHA before generating into new /run directories; preserve installed originals.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import base64
import json
from pathlib import Path
from threading import Event,RLock
import uuid
from .usb_managed import UsbController
from .usb_tx_managed import UsbTxController


class UsbStreamTxController(UsbTxController):
    """Low-level arbitrary-IQ session; derived controllers explicitly supply RF plans without falling back to RAMP32 ownership."""
    def prepare(self,mask):
        """Require complete eight-frame granularity; zero samples requires zero seconds for true unlimited operation."""
        options=self.native_ref().options()
        if options.tx_validation!='none' or options.tx_samples%16384 or options.tx_samples<0:
            raise ValueError('USB_ARBITRARY_GEOMETRY_OR_VALIDATION')
        if (options.tx_samples==0)!=(self.seconds==0):raise ValueError('USB_STREAMING_LENGTH_AND_DURATION')
        if options.tx_samples and (options.tx_samples<16384 or options.tx_samples>options.sample_rate*(self.seconds+1)):
            raise ValueError('USB_ARBITRARY_FINITE_RANGE')
        self.phase_lock=RLock();self.phase='initial';self.digital_end=Event()
        self.tx_observations=0;self.tx_snapshot=None;self.stop_expected=False
        actual=UsbController.prepare(self,mask)
        metadata=json.loads(actual.metadata_json);metadata.update(stream_source='native_usb_tx',tx_checker=0)
        actual.metadata_json=json.dumps(metadata,separators=(',',':'))
        self._save('candidate-runtime.json',dict(code_root=self.code_root,original_sdk_lock_sha256=
            __import__('hashlib').sha256((self.lock_path.parent/'h102-usb-sdk.lock.json').read_bytes()).hexdigest(),
            checker=0,RF_TX=getattr(self,'rf_enabled',False),continuous=not options.tx_samples))
        return actual

# End of file: usb_stream_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
