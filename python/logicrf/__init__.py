"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file __init__.py
@brief GNU Radio H102 construction interface with a C++ IQ data path.
@details Select mock/managed IIO explicitly; construction never connects/transmits; USB uses a separately managed process.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.0
@date 2026-10-16
"""
__version__ = "1.2.3"

from pathlib import Path
import os
from .logicrf_python import (device, device_options, stream_probe, fm_audio_sink,
                            h102_metadata_sink as _metadata_sink,
                            h102_rx_source as _rx_source, h102_tx_sink as _tx_sink,
                            tx_ramp_source as _tx_ramp_source)


def make_device(backend='mock', sample_rate=1e6, center_frequency=150e6,
                configuration='mock', uri='', mock_samples=0, queue_frames=None,
                username='logicrf', known_hosts=None, runtime=None, evidence=None,
                password_provider=None, machine_id=None, sample_limit=0, pattern='normal',
                usb_seconds=10, host_guard=None, host_password_provider=None,
                direction='rx', tx_samples=65536):
    """Construct a hardware-free Device; IIO prepares exclusive ownership after all Source.start calls aggregate.

    Frequency/sample rate are in Hz; sample_limit is finite IIO output per channel, with 0 for continuous.
    Fixed API connection needs only ip:device-IP; legacy credential providers are unused.
    """
    options = device_options()
    options.direction, options.tx_samples = direction, tx_samples
    if direction=='tx' and backend=='usb':
        if pattern not in ('ramp32','normal'):raise ValueError('USB_TX_INVALID_VALIDATION_PATTERN')
        options.tx_validation='ramp32' if pattern=='ramp32' else 'none'
    options.backend, options.uri, options.configuration = backend, uri, configuration
    options.sample_rate, options.center_frequency = sample_rate, center_frequency
    options.mock_samples, options.queue_frames = mock_samples, (512 if backend=='usb' else 64) if queue_frames is None else queue_frames
    options.sample_limit = sample_limit
    if backend in ('iio','usb'):
        if not uri.startswith('ip:') or not uri[3:]:
            raise ValueError('IIO requires explicit ip:host URI')
        # Keep low-rate blocks near 250 ms; the released 58 profiles start at 192 kSPS; reject unpublished requests.
        options.frame_samples = (min(65536, 1 << max(0, int(sample_rate/4).bit_length()-1))
                                 if sample_rate < 1e6 else 262144)
        options.frame_samples = max(1024, options.frame_samples)
        if direction=='tx' and backend=='usb':options.frame_samples=2048
        if direction=='tx' and backend=='iio':
            options.frame_samples=min(65536,options.frame_samples)
            if queue_frames is None:options.queue_frames=8
    native = device.make(options)
    if backend not in ('iio','usb'):
        return native
    from .managed import ManagedDevice, OwnerController, default_runtime
    project = Path(os.environ.get('LOGICRF_PROJECT_ROOT','.')).resolve()
    installed = Path(os.environ.get('LOGICRF_MODULE_PREFIX','.')).resolve()
    locks = installed/'share/gr-logicrf/locks'
    # Out-of-tree CTest uses matching source locks; installed locks reside in the install prefix.
    if not (locks/'h102-runtime.lock.json').is_file():
        locks = Path(__file__).resolve().parent/'locks'
    if backend=='usb':
        if direction=='tx':
            if pattern=='ramp32':from .usb_tx_managed import UsbTxController as Controller
            else:from .usb_stream_managed import UsbStreamTxController as Controller
        else:
            if pattern!='normal' or sample_limit:raise ValueError('USB_REQUIRES_NORMAL_TIMED_RX')
            from .usb_managed import UsbController as Controller
        controller=Controller(native,uri[3:],username,
            known_hosts or project/'envs/known_hosts',
            runtime or default_runtime(project,locks/'h102-runtime.lock.json'),
            locks/'h102-runtime.lock.json',locks/'h102-firmware.lock.json',
            evidence or project/'evidence/gr-usb',
            None if configuration in ('','active','mock') else configuration,
            password_provider,machine_id,project,
            host_guard or os.environ.get('LOGICRF_USB_HOST_GUARD','/usr/local/lib/logicrf/hawk-h102/gr-usb/p3b-05/usb_host_guard.py'),
            host_password_provider,usb_seconds)
        return ManagedDevice(native,controller)
    if direction=='tx':
        from .iio_tx_managed import IioTxController as Controller
    else:Controller=OwnerController
    controller = Controller(native,uri[3:],username,
        known_hosts or project/'envs/known_hosts',
        runtime or default_runtime(project,locks/'h102-runtime.lock.json'),
        locks/'h102-runtime.lock.json', locks/'h102-firmware.lock.json',
        evidence or project/'evidence/gr-iio',
        None if configuration in ('','active','mock') else configuration,
        password_provider,machine_id,pattern)
    return ManagedDevice(native,controller)


def h102_rx_source(board, channel):
    """Expand one managed Device into native single-channel Sources without construction IO."""
    native=getattr(board,'rx_native',getattr(board,'native',board))
    if hasattr(board,'rx_native'):
        if channel not in board.controller.rx_channels:raise ValueError('RX_CHANNEL_NOT_PLANNED')
        result=_rx_source(native,channel);board.controller.registered['rx'].add(channel);return result
    return _rx_source(native,channel)


def h102_tx_sink(board, channel):
    """Construct native TX Sink for finite/continuous user IQ; explicit managed sessions authorize RF."""
    if hasattr(board,'tx_native'):
        if channel not in board.controller.tx_channels:raise ValueError('TX_CHANNEL_NOT_PLANNED')
        result=_tx_sink(board.tx_native,channel);board.controller.registered['tx'].add(channel);return result
    return _tx_sink(getattr(board,'native',board),channel)


def make_duplex_device(*,backend='iio',rx_channels=(1,),tx_channels=(1,),
                       rx_frequency=1e9,tx_frequency=1e9,rf_enabled=False,
                       attenuation_db=40,rx_gain_db=None,**kwargs):
    """Create one board-owned IIO/USB RX/TX session; either direction may be empty; connect all channels; RF defaults off.

    Other parameters match make_device; tx_samples=0 is continuous. IIO per-direction/combined budget is 64 MB/s.
    USB supports single/dual one-way or native 1RX1TX mixed operation using one handle and 320 MB/s total; enable RF explicitly.
    """
    import math
    if backend not in ('iio','usb'):raise ValueError('DUPLEX_BACKEND_NOT_IMPLEMENTED')
    rx_channels=tuple(rx_channels);tx_channels=tuple(tx_channels)
    if rx_channels not in ((),(1,),(2,),(1,2)) or tx_channels not in ((),(1,),(2,),(1,2)) or not (rx_channels or tx_channels):
        raise ValueError('DUPLEX_CHANNEL_SELECTION')
    rate=kwargs.get('sample_rate',1e6)
    if backend=='iio':
        if rate*4*max(len(rx_channels),len(tx_channels))>64_000_000:raise ValueError('DUPLEX_NETWORK_BUDGET')
        if rate*4*(len(rx_channels)+len(tx_channels))>64_000_000:raise ValueError('DUPLEX_AGGREGATE_VALIDATED_BUDGET')
    else:
        if (rx_channels and tx_channels and (len(rx_channels)!=1 or len(tx_channels)!=1)) or rate*4*(len(rx_channels)+len(tx_channels))>320_000_000:
            raise ValueError('USB_DUPLEX_NATIVE_MODE_OR_BUDGET')
        if kwargs.get('sample_limit',0):raise ValueError('USB_DUPLEX_RX_LENGTH_FOLLOWS_TX_SESSION')
        if not kwargs.get('host_guard'):kwargs['host_guard']=os.environ.get('LOGICRF_USB_HOST_GUARD','/usr/local/lib/logicrf/hawk-h102/gr-usb/p3b-05/usb_host_guard.py')
    if rf_enabled and not tx_channels:raise ValueError('RF_REQUIRES_TX_CHANNELS')
    if type(rf_enabled) is not bool:raise ValueError('RF_EXPLICIT_BOOLEAN_REQUIRED')
    if not math.isfinite(attenuation_db) or not 20<=attenuation_db<=40 or abs(attenuation_db*20-round(attenuation_db*20))>1e-6:
        raise ValueError('TX_ATTENUATION_RANGE')
    gains=rx_gain_db or {}
    if any(ch not in (1,2) or not math.isfinite(gain) or not -3<=gain<=30 for ch,gain in gains.items()):raise ValueError('RX_GAIN_RANGE')
    if kwargs.get('pattern','normal')!='normal':raise ValueError('DUPLEX_NORMAL_IQ_ONLY')
    tx=make_device(backend=backend,direction='tx',center_frequency=tx_frequency,**kwargs) if tx_channels else None
    rx=make_device(backend=backend,direction='rx',center_frequency=rx_frequency,**kwargs) if rx_channels else None
    from .iio_stream_managed import IioStreamController,DuplexDevice
    primary=tx if tx else rx;c=primary.controller
    if backend=='usb':
        from .usb_duplex_managed import UsbDuplexController
        from .usb_radio_rx_managed import UsbRadioRxController
        Controller=UsbDuplexController if tx_channels else UsbRadioRxController
        controller=Controller(primary.native,c.host,c.username,c.known_hosts,c.runtime,c.lock_path,
            c.firmware_lock,c.evidence,c.profile,c.provider,c.machine_id,c.project,c.host_guard,c.host_provider,c.seconds,
            rx_native=rx.native if rx else None,rx_channels=rx_channels,tx_channels=tx_channels,
            rf_enabled=rf_enabled,attenuation_db=attenuation_db,rx_gain_db=gains)
    else:
        controller=IioStreamController(primary.native,c.host,c.username,c.known_hosts,c.runtime,c.lock_path,
            c.firmware_lock,c.evidence,c.profile,c.provider,c.machine_id,c.pattern,
            rx_native=rx.native if rx else None,rx_channels=rx_channels,tx_channels=tx_channels,
            rf_enabled=rf_enabled,attenuation_db=attenuation_db,rx_gain_db=gains)
    return DuplexDevice(tx.native if tx else None,rx.native if rx else None,controller)


def tx_ramp_source(board, channel):
    """Provide finite GNU digital test input from actual owner seed without direct USB access."""
    return _tx_ramp_source(getattr(board,'native',board),channel)


def h102_metadata_sink(board,path,channel):
    """Batch-write native USB integer metadata beside the same Source's cf32 File Sink without copying IQ."""
    native=getattr(board,'rx_native',getattr(board,'native',board))
    return _metadata_sink(native,str(path),channel)


from .control import h102_control,configuration_catalog,device_status,select_configuration

__all__ = ['__version__', 'device', 'device_options', 'make_device', 'make_duplex_device', 'h102_rx_source', 'h102_tx_sink', 'tx_ramp_source', 'stream_probe',
           'h102_control','configuration_catalog','device_status','select_configuration','h102_metadata_sink']
# End of file: __init__.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
