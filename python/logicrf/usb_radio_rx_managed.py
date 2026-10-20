"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_radio_rx_managed.py
@brief Silent frequency/gain plans and continuous USB single/dual RX.
@details Retain the same three-lock digital owner, FT IN process and independent post; freeze templates after actual tuning.
         Python controls only, never reads IQ; zero seconds removes absolute deadline but fresh short permits remain required.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
from threading import RLock
from .usb_managed import UsbController
from .usb_stream_managed import UsbStreamTxController


class UsbRadioRxController(UsbController):
    """RX-only shared ownership; register every selected channel before start; RF TX always stays off."""

    def __init__(self,*args,rx_native,rx_channels,tx_channels=(),rf_enabled=False,attenuation_db=40,rx_gain_db=None,**kwargs):
        super().__init__(*args,**kwargs)
        if tx_channels or rf_enabled:raise ValueError('USB_RX_ONLY_CONTROLLER')
        self.rx_channels=tuple(rx_channels);self.tx_channels=();self.radio_control=True
        self.rf_enabled=False;self.attenuation_db=attenuation_db;self.rx_gain_db={str(ch):value for ch,value in (rx_gain_db or {}).items()}
        self.prepared_options=None;self.registered={'rx':set(),'tx':set()};self.lifecycle=RLock()
        # GRC opt-in only: report native EOS first, then complete independent post on the same control thread.
        # This does not authorize restarting STOPPED objects or retaining a reusable USB data session.
        self.defer_finish=False

    def radio_plan(self):
        """Freeze this prepare's stopped plans; subsequent actual readback binds frame templates."""
        options=self.native_ref().options()
        return dict(configuration=self.info['active'],rf_enabled=False,rx_channels=list(self.rx_channels),tx_channels=[],
            rx_frequency=int(options.center_frequency),tx_frequency=int(options.center_frequency),
            attenuation_db=self.attenuation_db,rx_gain_db=self.rx_gain_db)

    def prepare_direction(self,direction,mask):
        """Start one native RX engine only; reject unconnected channels or duplicate independent ownership."""
        with self.lifecycle:
            if direction!='rx' or self.registered!={'rx':set(self.rx_channels),'tx':set()} or mask!=sum(1<<(ch-1) for ch in self.rx_channels):
                raise ValueError('USB_RX_DECLARED_CHANNELS_REQUIRE_BLOCKS')
            if self.prepared_options is None:self.prepared_options=super().prepare(mask)
            return self.prepared_options

    def finish_direction(self,direction):
        """Default full recovery; GRC may defer post to observe data stop separately from final release."""
        if not self.defer_finish:self.finish()

    def finish(self):
        """Clear preparation caches even on failure and propagate recovery failures unchanged."""
        try:super().finish()
        finally:self.prepared_options=None

# End of file: usb_radio_rx_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
