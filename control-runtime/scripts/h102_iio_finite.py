"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       h102_iio_finite.py
@brief      Capture one explicit-layout finite 65536-IQ channel using the host libiio 0.x network backend.
@details    The caller must hold the shared experiment lock and board RF ownership and supply a valid permission check.
            Do not set RF/gain/profile or offer a standalone CLI; finally destroys each buffer.
            ctypes signatures match ADI libiio v0.23 iio.h; reject non-0.x libraries, unknown scan layouts and incomplete data.
            Finite snapshots do not prove losslessness or sustained 40 MSPS Ethernet transport.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.1.0
@date       2026-10-01
"""
import ctypes as C
import ctypes.util
import time

SAMPLES = 65536


class Format(C.Structure):
    """Public ADI libiio 0.x C format structure with natural platform layout; no _pack_."""
    _fields_ = [('length', C.c_uint), ('bits', C.c_uint), ('shift', C.c_uint),
                ('is_signed', C.c_bool), ('is_fully_defined', C.c_bool),
                ('is_be', C.c_bool), ('with_scale', C.c_bool),
                ('scale', C.c_double), ('repeat', C.c_uint)]


class FiniteIIO:
    """Single-thread network context; use with for lifetime and retain the experiment lock.

    Check session permission at buffer creation and after refill; reject and clean up on expiry.
    Raise network/format/length errors without automatic retry or board/USB reset.
    """
    def __init__(self, uri: str, permit):
        self.permit = permit
        self.lib = C.CDLL(ctypes.util.find_library('iio'), use_errno=True)
        signatures = {
            'iio_library_get_version': (None, [C.POINTER(C.c_uint), C.POINTER(C.c_uint), C.c_char_p]),
            'iio_create_context_from_uri': (C.c_void_p, [C.c_char_p]),
            'iio_context_set_timeout': (C.c_int, [C.c_void_p, C.c_uint]),
            'iio_context_destroy': (None, [C.c_void_p]),
            'iio_context_get_xml': (C.c_char_p, [C.c_void_p]),
            'iio_context_find_device': (C.c_void_p, [C.c_void_p, C.c_char_p]),
            'iio_device_get_channels_count': (C.c_uint, [C.c_void_p]),
            'iio_device_get_channel': (C.c_void_p, [C.c_void_p, C.c_uint]),
            'iio_device_find_channel': (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_bool]),
            'iio_channel_disable': (None, [C.c_void_p]),
            'iio_channel_enable': (None, [C.c_void_p]),
            'iio_channel_get_index': (C.c_long, [C.c_void_p]),
            'iio_channel_get_data_format': (C.POINTER(Format), [C.c_void_p]),
            'iio_device_get_sample_size': (C.c_ssize_t, [C.c_void_p]),
            'iio_device_create_buffer': (C.c_void_p, [C.c_void_p, C.c_size_t, C.c_bool]),
            'iio_buffer_destroy': (None, [C.c_void_p]),
            'iio_buffer_refill': (C.c_ssize_t, [C.c_void_p]),
            'iio_buffer_step': (C.c_ssize_t, [C.c_void_p]),
            'iio_buffer_start': (C.c_void_p, [C.c_void_p]),
            'iio_buffer_end': (C.c_void_p, [C.c_void_p]),
            'iio_buffer_first': (C.c_void_p, [C.c_void_p, C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            method = getattr(self.lib, name); method.restype = result; method.argtypes = arguments
        major, minor, tag = C.c_uint(), C.c_uint(), C.create_string_buffer(8)
        self.lib.iio_library_get_version(C.byref(major), C.byref(minor), tag)
        if major.value != 0 or minor.value < 23:
            raise RuntimeError('LIBIIO_ABI')
        self.version = f'{major.value}.{minor.value}'
        if not permit():
            raise RuntimeError('OWNER_PERMISSION')
        self.context = self.lib.iio_create_context_from_uri(uri.encode())
        if not self.context:
            raise OSError(C.get_errno(), 'IIO_CONTEXT')
        if self.lib.iio_context_set_timeout(self.context, 750) < 0:
            self.close(); raise RuntimeError('IIO_TIMEOUT')
        self.xml = self.lib.iio_context_get_xml(self.context)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.close()

    def close(self) -> None:
        """Release context, with no effect if already closed; no live buffers may remain."""
        if self.context:
            self.lib.iio_context_destroy(self.context); self.context = None

    def capture(self, channel: int) -> tuple[bytes, dict]:
        """Return exactly 65536 LE IQ16 samples plus actual layout/duration; destroy buffers on every exit."""
        if type(channel) is not int or channel not in (1, 2) or not self.permit():
            raise ValueError('CHANNEL_OR_PERMISSION')
        name = ('axi-adrv9002-rx-lpc', 'axi-adrv9002-rx2-lpc')[channel - 1]
        device = self.lib.iio_context_find_device(self.context, name.encode())
        if not device:
            raise RuntimeError('RX_DEVICE')
        for index in range(self.lib.iio_device_get_channels_count(device)):
            self.lib.iio_channel_disable(self.lib.iio_device_get_channel(device, index))
        channels = [self.lib.iio_device_find_channel(device, name.encode(), False)
                    for name in ('voltage0_i', 'voltage0_q')]
        for index, entry in enumerate(channels):
            if not entry or self.lib.iio_channel_get_index(entry) != index:
                raise RuntimeError('SCAN_INDEX')
            data = self.lib.iio_channel_get_data_format(entry).contents
            if (data.length, data.bits, data.shift, data.is_signed, data.is_be, data.repeat) != (16, 16, 0, True, False, 1):
                raise RuntimeError('SCAN_TYPE')
            self.lib.iio_channel_enable(entry)
        if self.lib.iio_device_get_sample_size(device) != 4 or not self.permit():
            raise RuntimeError('SCAN_SIZE_OR_PERMISSION')
        start = time.monotonic()
        buffer = self.lib.iio_device_create_buffer(device, SAMPLES, False)
        if not buffer:
            raise OSError(C.get_errno(), 'IIO_BUFFER')
        try:
            size = self.lib.iio_buffer_refill(buffer)
            if size != SAMPLES * 4:
                raise RuntimeError('REFILL_LENGTH: ' + str(size))
            base = self.lib.iio_buffer_start(buffer)
            end = self.lib.iio_buffer_end(buffer)
            positions = [self.lib.iio_buffer_first(buffer, entry) - base for entry in channels]
            if self.lib.iio_buffer_step(buffer) != 4 or positions != [0, 2] or end - base != size:
                raise RuntimeError('BUFFER_LAYOUT')
            if not self.permit():
                raise RuntimeError('OWNER_EXPIRED_DURING_CAPTURE')
            raw = C.string_at(base, size)
        finally:
            self.lib.iio_buffer_destroy(buffer)
        return raw, dict(channel=channel, device=name, samples=SAMPLES, bytes=size,
                         scan_type='le:s16/16>>0', scan_order=['I', 'Q'], offsets=positions,
                         elapsed=time.monotonic() - start, library_version=self.version,
                         continuity_proven=False)

# End of file: h102_iio_finite.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
