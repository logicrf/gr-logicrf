"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       board_capture.py
@brief      Board-side short FFT capture and bounded ADC-to-DMA overflow verification.
@details    Run within an existing locked actor. Local libiio requires LE IQ16, an exclusive two-block DMA pool and stale-queue discard after tuning.
            Map the primary RX MMIO window covering both sticky status channels only after DT/version/BIT checks.
            Do not access secondary RX2 debugfs or enable TX; independent post handles exceptions and recovery.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.8.1
@date       2026-10-18
"""
import base64
import ctypes as C
import ctypes.util
import mmap
from pathlib import Path
import struct
import time


BIT_SHA256 = 'd81b6988b5f52b2fe1e03a86ba23508d7096560a9a60e011fd17c71c199974aa'
"""Original 1.2.0 release bitstream; keep this name for callers identifying its historical scope."""
SUPPORTED_BIT_SHA256 = frozenset((BIT_SHA256,
    'd6a2412cd32c767b29f08b077f9ee3eae159a5bf1188417db243e55ab33e3eff',
    'efa68686339a902c5aa6ffcd36db9f4e769b9dd5c796dcea75b6928f66fa33a2'))
"""Accept only D81, EFA and panel d6 bitstreams reviewed against the same ADC address/version/overflow ABI."""


class OverflowGuard:
    """One actor owns bounded MMIO; do not clear status during the window; end-check before destroying buffers.

    begin clears pre-warmup status once. arm verifies no overflow since begin after stale queues drain. Any overflow fails;
    do not clear and continue recording. This covers the selected BIT's ADC FIFO-to-DMA interval, not dual-channel synchronization.
    """
    def __init__(self, hw, identity):
        primary = hw.devices['axi-adrv9002-rx-lpc'].resolve().parent/'of_node'
        if (identity['boot_pins'].get('/boot/h102_logicrf_master.bit') not in SUPPORTED_BIT_SHA256
                or primary.joinpath('reg').read_bytes() != struct.pack('>II', 0x43c00000, 0x6000)
                or b'adi,axi-adrv9002-rx-1.0' not in primary.joinpath('compatible').read_bytes().split(b'\0')):
            raise RuntimeError('OVERFLOW_GUARD_UNKNOWN_ABI')
        self.hw = hw
        self.memory = None
        self.windows = {}
        with open('/dev/mem', 'r+b', buffering=0) as descriptor:
            self.memory = mmap.mmap(descriptor.fileno(), 0x6000, flags=mmap.MAP_SHARED,
                                    prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=0x43c00000)
        try:
            if any(self._read(offset) != 0x000a0300 for offset in (0, 0x1000)):
                raise RuntimeError('OVERFLOW_GUARD_CORE_VERSION')
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        """Release the sole mapping without changing hardware status."""
        if self.memory is not None:
            self.memory.close()
            self.memory = None

    def _read(self, offset):
        if offset not in (0, 0x88, 0x1000, 0x1088):
            raise ValueError('OVERFLOW_GUARD_OFFSET')
        return struct.unpack_from('<I', self.memory, offset)[0]

    def _status(self, channel):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError('OVERFLOW_GUARD_CHANNEL')
        return self._read((channel-1)*0x1000+0x88)

    def _active(self, channel):
        name = ('axi-adrv9002-rx-lpc', 'axi-adrv9002-rx2-lpc')[channel-1]
        if self.hw.read(self.hw.devices[name]/'buffer/enable') != '1':
            raise RuntimeError('OVERFLOW_GUARD_BUFFER_NOT_ACTIVE')

    def begin(self, channel):
        """Clear sticky status once before stale-queue discard; do not begin the same channel twice."""
        self._status(channel)
        self._active(channel)
        if channel in self.windows:
            raise RuntimeError('OVERFLOW_GUARD_ALREADY_STARTED')
        struct.pack_into('<I', self.memory, (channel-1)*0x1000+0x88, 4)
        # Allow CDC status accumulation and AXI readback; this delay is outside the recording window.
        time.sleep(.002)
        value = dict(channel=channel, cleared_ns=time.monotonic_ns(), stage='warming')
        self.windows[channel] = value
        return dict(value)

    def arm(self, channel):
        """After warmup drains, verify the conservative overflow-free window starting at begin, including warmup."""
        self._active(channel)
        value = self.windows[channel]
        if value['stage'] != 'warming' or self._status(channel) & 4:
            raise RuntimeError('OVERFLOW_GUARD_WARMUP_OVERFLOW')
        value.update(stage='armed', armed_ns=time.monotonic_ns(), initial_status=0, polls=0)
        return dict(value)

    def poll(self):
        """Raise immediately on any window overflow; retain sticky history."""
        for channel, value in self.windows.items():
            if value['stage'] != 'armed':
                continue
            self._active(channel)
            value['polls'] += 1
            if self._status(channel) & 4:
                raise RuntimeError(f'RX{channel}_ADC_DMA_OVERFLOW')

    def end(self, channel):
        """Save final state while receiving; accept only an established overflow-free window."""
        self._active(channel)
        value = self.windows[channel]
        status = self._status(channel)
        if value['stage'] != 'armed' or status & 4:
            raise RuntimeError(f'RX{channel}_ADC_DMA_OVERFLOW')
        value.update(stage='ended', checked_ns=time.monotonic_ns(), final_status=status,
                     passed=True, checked_before_buffer_destroy=True,
                     scope='pinned RTL ADC FIFO through AXI DMA; TCP and file checked by host')
        return dict(value)


class Format(C.Structure):
    """Public libiio 0.x data-format ABI with native platform layout."""
    _fields_ = [('length', C.c_uint), ('bits', C.c_uint), ('shift', C.c_uint),
                ('is_signed', C.c_bool), ('is_fully_defined', C.c_bool),
                ('is_be', C.c_bool), ('with_scale', C.c_bool),
                ('scale', C.c_double), ('repeat', C.c_uint)]


class LocalCapture:
    """One actor owns local context, bounded DMA pool and FFT windows; destroy buffers before restoring pool size."""
    def __init__(self, channel, points):
        import numpy as np
        self.np, self.points = np, points
        self.context = None
        self.device = None
        self.kernel_count_changed = False
        self.last_timings = {}
        self.fft_plan = None
        self.buffer = None
        self.channel = channel
        self.lib = C.CDLL(ctypes.util.find_library('iio'), use_errno=True)
        signatures = {
            'iio_library_get_version': (None, [C.POINTER(C.c_uint), C.POINTER(C.c_uint), C.c_char_p]),
            'iio_create_context_from_uri': (C.c_void_p, [C.c_char_p]),
            'iio_context_destroy': (None, [C.c_void_p]),
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
            'iio_buffer_set_blocking_mode': (C.c_int, [C.c_void_p, C.c_bool]),
            'iio_buffer_get_poll_fd': (C.c_int, [C.c_void_p]),
            'iio_device_set_kernel_buffers_count': (C.c_int, [C.c_void_p, C.c_uint]),
        }
        for name, (result, args) in signatures.items():
            method = getattr(self.lib, name)
            method.restype, method.argtypes = result, args
        major, minor, tag = C.c_uint(), C.c_uint(), C.create_string_buffer(8)
        self.lib.iio_library_get_version(C.byref(major), C.byref(minor), tag)
        if major.value != 0 or minor.value < 23:
            raise RuntimeError('LOCAL_LIBIIO_ABI')
        self.context = self.lib.iio_create_context_from_uri(b'local:')
        if not self.context:
            raise RuntimeError('LOCAL_IIO_CONTEXT')
        try:
            name = ('axi-adrv9002-rx-lpc', 'axi-adrv9002-rx2-lpc')[channel-1]
            self.device = self.lib.iio_context_find_device(self.context, name.encode())
            if not self.device:
                raise RuntimeError('LOCAL_RX_DEVICE')
            for index in range(self.lib.iio_device_get_channels_count(self.device)):
                self.lib.iio_channel_disable(self.lib.iio_device_get_channel(self.device, index))
            self.scan = [self.lib.iio_device_find_channel(self.device, n, False)
                         for n in (b'voltage0_i', b'voltage0_q')]
            for index, entry in enumerate(self.scan):
                if not entry or self.lib.iio_channel_get_index(entry) != index:
                    raise RuntimeError('LOCAL_SCAN_INDEX')
                data = self.lib.iio_channel_get_data_format(entry).contents
                if (data.length, data.bits, data.shift, data.is_signed, data.is_be, data.repeat) != (16, 16, 0, True, False, 1):
                    raise RuntimeError('LOCAL_SCAN_TYPE')
                self.lib.iio_channel_enable(entry)
            if self.lib.iio_device_get_sample_size(self.device) != 4:
                raise RuntimeError('LOCAL_SCAN_SIZE')
            if self.lib.iio_device_set_kernel_buffers_count(self.device,2)<0:
                raise RuntimeError('LOCAL_KERNEL_BUFFERS')
            self.kernel_count_changed=True
            self.window=np.hanning(points).astype(np.float32)
            self.offsets=np.fft.fftshift(np.fft.fftfreq(points,1))
            self.normalizer=np.float32(self.window.sum()*32768)
            self.fft_input=np.empty(points,dtype=np.complex64)
            self.fft_output=np.empty(points,dtype=np.complex64)
            self.fftw=C.CDLL(ctypes.util.find_library('fftw3f'))
            for name,result,args in (
                    ('fftwf_plan_dft_1d',C.c_void_p,[C.c_int,C.c_void_p,C.c_void_p,C.c_int,C.c_uint]),
                    ('fftwf_execute',None,[C.c_void_p]),('fftwf_destroy_plan',None,[C.c_void_p])):
                method=getattr(self.fftw,name);method.restype=result;method.argtypes=args
            self.fft_plan=self.fftw.fftwf_plan_dft_1d(points,self.fft_input.ctypes.data,
                self.fft_output.ctypes.data,-1,64)
            if not self.fft_plan:raise RuntimeError('LOCAL_FFTW_PLAN')
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        """Release buffers before closing context; failures do not alter RF attributes."""
        if self.fft_plan:
            self.fftw.fftwf_destroy_plan(self.fft_plan);self.fft_plan=None
        if self.buffer:
            self.lib.iio_buffer_destroy(self.buffer);self.buffer=None
        if self.context:
            result=self.lib.iio_device_set_kernel_buffers_count(self.device,4) if self.kernel_count_changed else 0
            self.lib.iio_context_destroy(self.context)
            self.context = None
            if result<0:raise RuntimeError('LOCAL_KERNEL_BUFFERS_RESTORE')

    def power(self, averages, check):
        """Capture spectrum from the same two-block DMA pool; discard three blocks after each retune.

        This object exclusively owns DMA. Retuning IQ is invalid: discard two queued blocks and one RF
        transition block before the FFT. This path does not provide sweep IQ continuity or recording.
        """
        import select
        np=self.np;started=time.monotonic();refill_seconds=0;fft_seconds=0
        if self.buffer is None:
            self.buffer=self.lib.iio_device_create_buffer(self.device,self.points,False)
            if not self.buffer:raise OSError(C.get_errno(),'LOCAL_IIO_BUFFER')
            if self.lib.iio_buffer_set_blocking_mode(self.buffer,False)<0:
                raise RuntimeError('LOCAL_IIO_NONBLOCKING')
        buffer=self.buffer;created=time.monotonic()
        fd=self.lib.iio_buffer_get_poll_fd(buffer)
        if fd<0:raise RuntimeError('LOCAL_IIO_POLL_FD')
        total=np.zeros(self.points)
        for index in range(averages+3):
            refill_started=time.monotonic();deadline=refill_started+2
            while True:
                check();size=self.lib.iio_buffer_refill(buffer)
                if size!=-11:break
                if time.monotonic()>=deadline:raise TimeoutError('LOCAL_IIO_REFILL_DEADLINE')
                select.select([fd],[],[],.02)
            base=self.lib.iio_buffer_start(buffer)
            if (size!=self.points*4 or self.lib.iio_buffer_step(buffer)!=4
                    or self.lib.iio_buffer_end(buffer)-base!=size
                    or [self.lib.iio_buffer_first(buffer,s)-base for s in self.scan]!=[0,2]):
                raise RuntimeError('LOCAL_IIO_LAYOUT_LENGTH')
            refill_seconds+=time.monotonic()-refill_started
            if index>=3:
                fft_started=time.monotonic()
                values=np.frombuffer(C.string_at(base,size),dtype='<i2').reshape(-1,2)
                np.multiply(values[:,0],self.window,out=self.fft_input.real)
                np.multiply(values[:,1],self.window,out=self.fft_input.imag)
                self.fftw.fftwf_execute(self.fft_plan)
                spectrum=self.fft_output
                total+=np.fft.fftshift((spectrum.real*spectrum.real+spectrum.imag*spectrum.imag)/(self.normalizer*self.normalizer))
                fft_seconds+=time.monotonic()-fft_started
        self.last_timings=dict(create_seconds=created-started,refill_seconds=refill_seconds,
                               fft_seconds=fft_seconds,persistent_dma=True,discarded_blocks=3)
        return total/averages

    def verify_sources(self,hw):
        """Keep only local exclusive DMA active; the other RX, all TX and custom USB sources must be idle."""
        names=('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc')
        for channel,name in enumerate(names,1):
            expected='1' if self.buffer and channel==self.channel else '0'
            if hw.read(hw.devices[name]/'buffer/enable')!=expected:
                raise RuntimeError('SWEEP_FOREIGN_RX_BUFFER')
        base=0x43c50000
        resource=Path('/sys/bus/platform/devices/43c50000.axi_reg/of_node/reg')
        if resource.read_bytes()!=struct.pack('>II',base,0x10000):raise RuntimeError('SWEEP_CUSTOM_MMIO_RESOURCE')
        with open('/dev/mem','rb',buffering=0) as descriptor:
            with mmap.mmap(descriptor.fileno(),0x10000,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ,offset=base) as memory:
                values={x:struct.unpack_from('<I',memory,x)[0] for x in
                        (0,0x208,0x218,0x400,0x200,0x20c,0x30c,0x404,0x40c,0x604,0x60c,0x620)}
        if any(values[x]!=v for x,v in ((0,0x23070101),(0x208,0x42535548),(0x218,0x33474244),(0x400,0x33565852))):
            raise RuntimeError('SWEEP_CUSTOM_MMIO_ABI')
        if any(values[x] for x in (0x200,0x20c,0x404,0x40c,0x604,0x60c,0x620)) or values[0x30c]&9:
            raise RuntimeError('SWEEP_CUSTOM_CAPTURE_BUSY')


def grid_cells(power, center, plan, offsets=None):
    """Reduce to final-bin peaks for host stitching without losing narrow peaks in finer FFT bins."""
    import numpy as np
    fs, points = plan['sample_rate_hz'], plan['fft_points']
    offsets = np.fft.fftshift(np.fft.fftfreq(points, 1/fs)) if offsets is None else offsets*fs
    start, stop, size = plan['start_hz'], plan['stop_hz'], plan['grid_size']
    spacing = (stop-start)/(size-1)
    frequencies = center+offsets
    valid = ((abs(offsets) <= .4*min(plan['bandwidth_hz'], fs)+1)
             & (frequencies >= start) & (frequencies <= stop))
    if spacing > fs/points*1.01:
        indices = np.rint((frequencies[valid]-start)/spacing).astype(int)
        first, last = int(indices.min()), int(indices.max())
        cells = np.full(last-first+1, -np.inf)
        starts=np.r_[0,np.flatnonzero(np.diff(indices))+1]
        cells[indices[starts]-first]=np.maximum.reduceat(power[valid],starts)
        cells[~np.isfinite(cells)] = np.nan
    else:
        first = max(0, int(np.ceil((max(start, center-.4*min(plan['bandwidth_hz'],fs))-start)/spacing)))
        last = min(size-1, int(np.floor((min(stop, center+.4*min(plan['bandwidth_hz'],fs))-start)/spacing)))
        grid = start+np.arange(first, last+1)*spacing
        cells = np.interp(grid, frequencies, power)
    return dict(grid_first=first, power_f32le_b64=base64.b64encode(cells.astype('<f4').tobytes()).decode())


# End of file: board_capture.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
