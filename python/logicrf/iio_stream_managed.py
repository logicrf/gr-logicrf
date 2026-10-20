"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_stream_managed.py
@brief Aggregate IIO RX/TX under one board permission while IQ uses two native C++ engines.
@details Register channels without connecting; first direction prepares once; final direction performs independent post.
         Failures revoke both directions; configure while stopped; no cross-DMA sample-synchronization guarantee.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import json
from threading import RLock
import weakref

from .iio_tx_managed import IioTxController
from .managed import ManagedDevice,OwnerController,configure_native


class IioStreamController(IioTxController):
    """One board owner with serialized lifecycle; health thread renews two-second C++ permits only.

    Freeze RX/TX channels at construction and connect every declared block before start; never substitute backends.
    RF defaults off. Keep actual readback; recovery results remain separate from network submission checks.
    """
    payload_names=IioTxController.payload_names+('board_capture.py',)
    entrypoint='board_iio_stream.py'
    allow_tuning=True

    def __init__(self,*args,rx_native=None,rx_channels=(),tx_channels=(1,),
                 rf_enabled=False,attenuation_db=40,rx_gain_db=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.rx_ref=weakref.ref(rx_native) if rx_native else lambda:None
        self.rx_channels=tuple(rx_channels);self.tx_channels=tuple(tx_channels)
        self.rf_enabled=rf_enabled;self.attenuation_db=attenuation_db
        self.rx_gain_db={str(ch):value for ch,value in (rx_gain_db or {}).items()}
        self.lifecycle=RLock();self.prepared_options=None;self.finished_directions=set()
        self.registered={'rx':set(),'tx':set()}

    def expected_request(self,options,channels):
        """Freeze both directions' clock/LO/gain plans without treating requested values as readback."""
        request=super().expected_request(options,channels);rx=self.rx_ref()
        request.update(channels=list(self.tx_channels),samples=options.tx_samples if self.tx_channels else 0,rx_channels=list(self.rx_channels),rx_frequency=int(rx.options().center_frequency) if rx else int(options.center_frequency),
            rf_enabled=self.rf_enabled,attenuation_db=self.attenuation_db,rx_gain_db=self.rx_gain_db)
        return request

    def prepare_direction(self,direction,mask):
        """First direction prepares shared ownership; one request pins both source sets and identity."""
        with self.lifecycle:
            planned={'rx':set(self.rx_channels),'tx':set(self.tx_channels)}
            if self.registered!=planned:raise RuntimeError('DUPLEX_ALL_DECLARED_CHANNELS_REQUIRE_BLOCKS')
            if mask!=sum(1<<(ch-1) for ch in planned[direction]):raise RuntimeError('DUPLEX_NATIVE_CHANNEL_MASK')
            if self.native_ref().options().sample_rate*4*(len(self.rx_channels)+len(self.tx_channels))>64_000_000:
                raise ValueError('DUPLEX_AGGREGATE_VALIDATED_BUDGET')
            if self.prepared_options is None:
                self.finished_directions.clear()
                self.prepared_options=super().prepare(sum(1<<(ch-1) for ch in (self.tx_channels or self.rx_channels)))
            if direction=='tx':return self.prepared_options
            native=self.rx_ref();options=native.options()
            options.boot_id=self.prepared_options.boot_id;options.configuration=self.info['active']
            params={p['channel']:p for p in self.info['parameters']['rx']}
            if any(params[ch]['sample_rate_hz']!=options.sample_rate for ch in self.rx_channels):
                raise RuntimeError('DUPLEX_ACTUAL_RX_RATE_MISMATCH')
            options.rf_bandwidth=params[self.rx_channels[0]]['rf_bandwidth_hz']
            options.metadata_json=json.dumps(dict(self.info,RF_TX=self.rf_enabled),separators=(',',':'))
            native.renew_lease();return options

    def _monitor(self):
        """Revoke both directions on board-health loss so one side cannot continue transmitting after the other disconnects."""
        while not self.done.wait(.5):
            faults=[native.error() for native in (self.native_ref(),self.rx_ref()) if native is not None and native.error()]
            for native in (self.native_ref(),self.rx_ref()):
                if native is None:continue
                if faults:native.invalidate('DUPLEX_PEER_FAULT:'+faults[0])
                elif not self.link or not self.link.permitted():native.invalidate('OWNER_PERMISSION_EXPIRED')
                elif not self.pause_renewal:native.renew_lease()
            if faults or not self.link or not self.link.permitted():break

    def guard(self,channel,stage):
        """RX retains actual ADC overflow windows; TX disables selected RF at end without invented SSI counts."""
        with self.command_lock:
            if not self.link or not self.link.permitted():raise RuntimeError('STREAM_OWNER_PERMISSION_EXPIRED')
            if stage in ('begin','arm','end'):
                result=self.link.guard(stage,channel);key='rx'+str(channel)
            elif stage in ('iio-tx-begin','iio-tx-end'):
                result=(self.link._command(dict(action='tx_end',channel=channel))['guard']
                        if stage=='iio-tx-end' else dict(permitted=True,ssi_consumption_verified=False))
                key='tx'+str(channel)
            else:raise ValueError('IIO_STREAM_GUARD_STAGE')
            self.guard_records.setdefault(key,{})[stage]=result;self._save('guards.json',self.guard_records)

    def finish_direction(self,direction):
        """Restore after final-direction buffer destruction; close explicitly handles an unstarted peer direction."""
        with self.lifecycle:
            self.finished_directions.add(direction)
            expected=({'tx'} if self.tx_channels else set())|({'rx'} if self.rx_channels else set())
            if self.finished_directions>=expected:self.finish()

    def finish(self):
        """Forced cleanup retains independent post failures; clear runtime caches to allow graph restart."""
        try:OwnerController.finish(self)
        finally:self.prepared_options=None


class DuplexDevice(ManagedDevice):
    """User-held bidirectional Device with two C++ engines and one managed owner.

    Main control thread configures/closes without racing start; IQ never passes through this object.
    """
    def __init__(self,tx_native,rx_native,controller):
        self.native=tx_native if tx_native is not None else rx_native;self.tx_native=tx_native;self.rx_native=rx_native;self.controller=controller
        for direction,native in (('rx',rx_native),('tx',tx_native)):
            if native is not None:native.set_session(
                lambda mask,d=direction:controller.prepare_direction(d,mask),controller.guard,
                lambda d=direction:controller.finish_direction(d))

    def error(self):
        return ';'.join(v for v in (self.tx_native.error() if self.tx_native else '',self.rx_native.error() if self.rx_native else '') if v)

    def ready(self):
        return (self.tx_native is None or self.tx_native.ready()) and (self.rx_native is None or self.rx_native.ready())

    def queue_stats(self,channel):
        return self.rx_native.queue_stats(channel)

    def received(self,channel):
        return self.rx_native.received(channel)

    def stop_rx(self):
        """Stop/drain RX after actual complete IIO blocks; TX lifecycle stays independent."""
        if self.rx_native is None:raise RuntimeError('DUPLEX_HAS_NO_RX')
        if self.options().backend=='usb':
            return self.tx_native.request_tx_stop() if self.tx_native else self.controller.guard(0,'usb-stop')
        self.rx_native.request_rx_stop()

    def shutdown(self):
        """Explicit cancellation retains recovery errors; USB TX guard obtains board STOP/ACK while RX keeps draining.

        C++ TX retains its socket until guard returns; early RX cancellation can falsely fail FT601 output queues.
        IIO revokes both directions directly; independent post restores RF/configuration.
        """
        active=self.controller.prepared_options is not None
        if active and self.options().backend=='usb' and self.tx_native:
            self.controller.cancelling=True
            self.tx_native.invalidate('DUPLEX_CANCELLED_BY_SHUTDOWN')
            self.tx_native.shutdown()
            if self.rx_native:
                self.rx_native.invalidate('DUPLEX_CANCELLED_BY_SHUTDOWN');self.rx_native.shutdown()
        else:
            if active:
                for native in (self.tx_native,self.rx_native):
                    if native is not None:native.invalidate('DUPLEX_CANCELLED_BY_SHUTDOWN')
            for native in (self.tx_native,self.rx_native):
                if native is not None:native.shutdown()
        self.controller.finish()

    def close(self):
        """Normal completion adds no cancellation; raise data/recovery failures."""
        for native in (self.tx_native,self.rx_native):
            if native is not None:native.shutdown()
        self.controller.finish()
        if self.error():raise RuntimeError(self.error())

    def configure(self,profile,sample_rate,center_frequency=None,tx_frequency=None):
        """Stopped plans only; both Fs values must match one profile; never change active data rates."""
        if self.controller.prepared_options is not None:raise RuntimeError('STOP_REQUIRED_FOR_CONFIGURE')
        limit=320_000_000 if self.options().backend=='usb' else 64_000_000
        if sample_rate*4*(len(self.controller.rx_channels)+len(self.controller.tx_channels))>limit:
            raise ValueError('DUPLEX_AGGREGATE_VALIDATED_BUDGET')
        rx_frequency=self.rx_native.options().center_frequency if center_frequency is None and self.rx_native else center_frequency
        tx_frequency=self.tx_native.options().center_frequency if tx_frequency is None and self.tx_native else tx_frequency
        # Validate both directions before changing either plan, avoiding half-updated state if the second direction fails.
        from .logicrf_python import device
        from .managed import iio_frame_samples
        for native,frequency in ((self.tx_native,tx_frequency),(self.rx_native,rx_frequency)):
            if native is None:continue
            options=native.options();options.configuration=profile;options.sample_rate=sample_rate;options.center_frequency=frequency
            if options.backend=='iio':options.frame_samples=iio_frame_samples(sample_rate,options.direction)
            device.make(options)
        if self.tx_native:configure_native(self.tx_native,profile,sample_rate,tx_frequency)
        if self.rx_native:configure_native(self.rx_native,profile,sample_rate,rx_frequency)
        self.controller.profile=profile or None

    def set_rx_gain(self,channel,gain_db):
        """Plan selected RX SPI gain in dB while stopped; read actual driver state at start."""
        import math
        if self.controller.prepared_options is not None:raise RuntimeError('STOP_REQUIRED_FOR_GAIN')
        if channel not in self.controller.rx_channels or not math.isfinite(gain_db) or not -3<=gain_db<=30:raise ValueError('RX_GAIN_RANGE_OR_CHANNEL')
        self.controller.rx_gain_db[str(channel)]=gain_db

    def set_tx_attenuation(self,attenuation_db):
        """Plan 20-40 dB attenuation in 0.05 dB steps while stopped; explicit session plans still control RF."""
        import math
        if self.controller.prepared_options is not None:raise RuntimeError('STOP_REQUIRED_FOR_GAIN')
        if not self.controller.tx_channels or not math.isfinite(attenuation_db) or not 20<=attenuation_db<=40 or abs(attenuation_db*20-round(attenuation_db*20))>1e-6:
            raise ValueError('TX_ATTENUATION_RANGE')
        self.controller.attenuation_db=attenuation_db

    def set_center_frequency(self,frequency):
        """Set RX plans while stopped; independent TX LO uses configure's tx_frequency."""
        if self.rx_native:self.rx_native.set_center_frequency(frequency)
        else:self.tx_native.set_center_frequency(frequency)

# End of file: iio_stream_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
