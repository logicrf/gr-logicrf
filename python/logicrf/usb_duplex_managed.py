"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_duplex_managed.py
@brief Aggregate 1RX1TX under one native board owner and one FT601 process.
@details One frozen request binds both templates; RUN/PLAY starts after IN reads are posted and TX prefilled.
         After END keep reading IN through STOP/DRAIN/ACK; independent recovery follows final-direction exit.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import hashlib
import json
from threading import RLock
import time
import uuid
import weakref
from .usb_stream_managed import UsbStreamTxController


class UsbDuplexController(UsbStreamTxController):
    """Two C++ engines, one FT handle and one three-lock board owner; native 1RX1TX combinations only.

    RF requires explicit enable; private runtime handles silent tuning/recovery. Register channels before start; construction has no network/USB IO.
    """
    def __init__(self,*args,rx_native=None,rx_channels=(),tx_channels=(1,),rf_enabled=False,attenuation_db=40,rx_gain_db=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.rx_ref=weakref.ref(rx_native) if rx_native else lambda:None;self.rx_channels=tuple(rx_channels);self.tx_channels=tuple(tx_channels)
        self.lifecycle=RLock();self.prepared_options=None;self.rx_options=None;self.finished_directions=set()
        self.registered={'rx':set(),'tx':set()};self.receiver_ready=False;self.start_failure=None;self.cancelling=False
        self.radio_control=True;self.rf_enabled=rf_enabled;self.attenuation_db=attenuation_db
        self.rx_gain_db={str(ch):value for ch,value in (rx_gain_db or {}).items()}

    def radio_plan(self):
        """Freeze current stopped-state plans on each prepare; never cache old profiles/carriers."""
        primary=self.native_ref().options();rx=self.rx_ref()
        return dict(configuration=self.info['active'],rf_enabled=self.rf_enabled,
            rx_channels=list(self.rx_channels),tx_channels=list(self.tx_channels),
            rx_frequency=int(rx.options().center_frequency) if rx else int(primary.center_frequency),
            tx_frequency=int(primary.center_frequency),attenuation_db=self.attenuation_db,rx_gain_db=self.rx_gain_db)

    def native_devices(self):
        return tuple(native for native in (self.native_ref(),self.rx_ref()) if native is not None)

    def masks(self,mask,transmit):
        return sum(1<<(ch-1) for ch in self.rx_channels),sum(1<<(ch-1) for ch in self.tx_channels)

    def validate_extra(self,state):
        """Verify actual peer-direction parameters before templates; TX readback cannot substitute for RX."""
        if self.rx_ref() is None:return
        options=self.rx_ref().options();params={p['channel']:p for p in state['info']['parameters']['rx']}
        if options.sample_rate!=self.native_ref().options().sample_rate:raise ValueError('USB_DUPLEX_COMMON_CLOCK_REQUIRED')
        for ch in self.rx_channels:
            if params[ch]['sample_rate_hz']!=options.sample_rate:raise ValueError('USB_DUPLEX_RX_RATE')
            if not self.radio_control and state['info']['radio']['rx_center_frequency_hz'][str(ch)]!=options.center_frequency:
                raise ValueError('USB_DUPLEX_REQUIRES_CURRENT_RX_CENTER')

    def host_request(self,options,mask,transmit):
        """Pass two exact private sockets to one SDK process; reject multiple processes reopening FT601."""
        request=super().host_request(options,mask,transmit)
        if not self.rx_channels:return request
        request.update(direction='duplex',rx_socket=self.rx_socket,
            rx_mask=sum(1<<(ch-1) for ch in self.rx_channels))
        return request

    def prepare_direction(self,direction,mask):
        """Prepare once and distribute actual templates/stream IDs; failure never restarts a peer owner."""
        with self.lifecycle:
            if self.start_failure:raise RuntimeError('USB_DUPLEX_START_FAILED:'+self.start_failure)
            planned={'rx':set(self.rx_channels),'tx':set(self.tx_channels)}
            if self.registered!=planned:raise ValueError('DUPLEX_ALL_DECLARED_CHANNELS_REQUIRE_BLOCKS')
            if mask!=sum(1<<(ch-1) for ch in planned[direction]):raise ValueError('USB_DUPLEX_NATIVE_MASK')
            if self.prepared_options is None:
                self.finished_directions.clear();self.receiver_ready=False;self.cancelling=False
                self.rx_socket=str(self.project/'envs/gr-usb'/(uuid.uuid4().hex+'.sock'))
                try:
                    tx=super().prepare(sum(1<<(ch-1) for ch in self.tx_channels))
                    self.prepared_options=tx
                    if self.rx_channels:
                        rx=self.rx_ref().options();config=bytes.fromhex(self.ready['rx_configuration_hex'])
                        if len(config)!=112 or hashlib.sha256(config).hexdigest()!=self.expected['rx_sha256']:
                            raise RuntimeError('USB_DUPLEX_RX_CONFIGURATION_SHA')
                        path=self.run_output/'rx-configuration.bin';path.write_bytes(config)
                        rx.boot_id=tx.boot_id;rx.configuration=tx.configuration;rx.usb_socket=self.rx_socket
                        rx.usb_configuration=str(path);rx.usb_stream_id=int(self.expected['rx_stream_id'])
                        rx.usb_time_epoch=self.expected['configuration_id']
                        params={p['channel']:p for p in self.info['parameters']['rx']}
                        rx.rf_bandwidth=params[self.rx_channels[0]]['rf_bandwidth_hz']
                        if self.radio_control and any(self.info['radio']['rx_center_frequency_hz'][str(ch)]!=int(rx.center_frequency) for ch in self.rx_channels):
                            raise RuntimeError('USB_DUPLEX_ACTUAL_RX_FREQUENCY')
                        rx.metadata_json=json.dumps(dict(self.info,stream_source='native_usb_duplex_rx',RF_TX=self.rf_enabled),separators=(',',':'))
                        self.rx_options=rx
                    self.renew_local()
                except Exception as error:
                    self.start_failure=str(error);self.invalidate_local('USB_DUPLEX_PREPARE:'+str(error));self.finish();raise
            return self.prepared_options if direction=='tx' else self.rx_options

    def _step(self,status,reply):
        """Confirm actual IN read posting/receiver_ready before ARM; no delayed legacy READY after ARMED."""
        if self.cancelling:return
        errors=[native.error() for native in self.native_devices() if native.error()]
        if errors:raise RuntimeError('USB_DUPLEX_PEER_FAULT:'+errors[0])
        if self.rx_channels and self.phase=='queue' and not self.receiver_ready:return
        super()._step(status,reply)

    def guard(self,channel,stage):
        """RX supplies actual IN readiness only; TX prefill state machine exclusively starts acquisition."""
        if stage=='usb-start':
            deadline=time.monotonic()+15
            while True:
                if self.failure:raise RuntimeError(self.failure)
                state=self._host('status')['state.json'] or {}
                if state.get('rx_queued'):break
                if time.monotonic()>deadline:raise TimeoutError('USB_DUPLEX_RX_QUEUED_TIMEOUT')
                time.sleep(.02)
            with self.phase_lock:
                self._command('receiver_ready',stream_id=self.expected['rx_stream_id'],configuration_sha256=self.expected['rx_sha256'])
                self.receiver_ready=True
            while not self.started:
                if self.failure:raise RuntimeError(self.failure)
                if time.monotonic()>deadline:raise TimeoutError('USB_DUPLEX_START_TIMEOUT')
                time.sleep(.02)
            return
        if stage=='usb-stop':
            if not self.board_stopped:self._stop_pair(cancel=True)
            return
        return super().guard(channel,stage)

    def finish_direction(self,direction):
        """Close native ownership after both directions leave; forced close also handles unstarted peers independently."""
        with self.lifecycle:
            self.finished_directions.add(direction)
            expected=({'rx'} if self.rx_channels else set())|({'tx'} if self.tx_channels else set())
            if self.finished_directions>=expected:self.finish()

    def finish(self):
        try:super().finish()
        finally:self.prepared_options=None;self.rx_options=None

# End of file: usb_duplex_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
