"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       tx_cyclic.py
@brief      Hold explicit-layout single-channel cyclic IQ16 in a local/network libiio 0.x buffer.
@details    Inherit FiniteIIO context/permission/timeout and strictly verify output scan layout.
            The caller retains board ownership and checks permission around create/push. Destroy buffers at close; no RF/DDS control.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.6.0
@date       2026-10-12
"""
import ctypes as C
import time
from h102_iio_finite import FiniteIIO, SAMPLES



class CyclicTX(FiniteIIO):
    """Single-thread cyclic TX with exclusive context/buffer and permission supplied by the actual outer owner.

    A successful push proves submission only, not chip consumption or RF output.
    """
    def __init__(self, uri: str, permit):
        """Initialize the owned buffer set before the base class checks actual permission and context."""
        self.buffers = []
        super().__init__(uri, permit)

    def close(self) -> None:
        """Destroy every cyclic buffer before context release; attempt all cleanup and propagate the first error."""
        errors = []
        while self.buffers:
            try:
                self.lib.iio_buffer_destroy(self.buffers.pop())
            except Exception as error:
                errors.append(error)
        super().close()
        if errors:
            raise errors[0]

    def transmit(self, channel: int, raw: bytes) -> dict:
        """Submit/retain one cyclic 65536-IQ block; destroy immediately on layout/length/permission errors without repush."""
        if type(channel) is not int or channel not in (1, 2) or len(raw) != SAMPLES * 4 or not self.permit():
            raise ValueError('TX_INPUT_OR_PERMISSION')
        push = self.lib.iio_buffer_push
        push.argtypes, push.restype = [C.c_void_p], C.c_ssize_t
        name = ('axi-adrv9002-tx-lpc', 'axi-adrv9002-tx2-lpc')[channel-1]
        device = self.lib.iio_context_find_device(self.context, name.encode())
        if not device:
            raise RuntimeError('TX_DEVICE')
        for i in range(self.lib.iio_device_get_channels_count(device)):
            self.lib.iio_channel_disable(self.lib.iio_device_get_channel(device, i))
        channels = [self.lib.iio_device_find_channel(device, n.encode(), True) for n in ('voltage0', 'voltage1')]
        for i, entry in enumerate(channels):
            if not entry or self.lib.iio_channel_get_index(entry) != i:
                raise RuntimeError('TX_SCAN_INDEX')
            f = self.lib.iio_channel_get_data_format(entry).contents
            if (f.length,f.bits,f.shift,f.is_signed,f.is_be,f.repeat) != (16,16,0,True,False,1):
                raise RuntimeError('TX_SCAN_TYPE')
            self.lib.iio_channel_enable(entry)
        if self.lib.iio_device_get_sample_size(device) != 4 or not self.permit():
            raise RuntimeError('TX_SCAN_SIZE_OR_PERMISSION')
        started = time.monotonic()
        buffer = self.lib.iio_device_create_buffer(device, SAMPLES, True)
        if not buffer:
            raise OSError(C.get_errno(), 'TX_BUFFER')
        try:
            base, end = self.lib.iio_buffer_start(buffer), self.lib.iio_buffer_end(buffer)
            positions = [self.lib.iio_buffer_first(buffer, entry)-base for entry in channels]
            if self.lib.iio_buffer_step(buffer) != 4 or positions != [0,2] or end-base != len(raw):
                raise RuntimeError('TX_BUFFER_LAYOUT')
            if not self.permit():
                raise RuntimeError('TX_PERMISSION_BEFORE_PUSH')
            C.memmove(base, raw, len(raw))
            size = push(buffer)
            if size != len(raw):
                raise RuntimeError('TX_PUSH_LENGTH: ' + str(size))
            if not self.permit():
                raise RuntimeError('TX_PERMISSION_AFTER_PUSH')
        except BaseException:
            self.lib.iio_buffer_destroy(buffer)
            raise
        self.buffers.append(buffer)
        return dict(channel=channel,device=name,samples=SAMPLES,bytes=size,cyclic=True,
                    scan_type='le:s16/16>>0',scan_order=['I','Q'],offsets=positions,
                    elapsed=time.monotonic()-started,library_version=self.version,
                    rf_output_proven=False,consumption_continuity_proven=False)

# End of file: tx_cyclic.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
