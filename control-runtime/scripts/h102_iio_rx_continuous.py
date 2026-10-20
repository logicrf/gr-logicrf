"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       h102_iio_rx_continuous.py
@brief      Check network throughput and RAMP16 boundaries with persistent libiio buffers under actual RX-only permission.
@details    Each channel owns a context. The caller holds all three board locks and independently checks actual permission.
            Strict LE IQ16 scanning validates within/across blocks; retain first/last/failed blocks and whole-stream hashes only.
            RX threads refill continuously into a bounded four-block queue; validation threads receive no hardware pointers.
            Host finally destroys buffers on failure; board deadlines and independent post handle disconnected recovery.
            16-bit RAMP has a 65536-sample ambiguity; it cannot establish absolute losslessness or RF/time qualification.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    1.1.0
@date       2026-10-08
"""
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Full
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from h102_iio_finite import FiniteIIO, SAMPLES


class ContinuousRX(FiniteIIO):
    """Own one RX buffer only; use with to release context and never share instances across threads.

    permit must come from a bounded monitor verified against the board; no standalone ownerless CLI.
    """
    def __init__(self,uri:str,permit):
        """Create context with the existing 0.x ABI and add the public kernel-buffer-count signature."""
        super().__init__(uri,permit)
        method=self.lib.iio_device_set_kernel_buffers_count
        method.argtypes=[C.c_void_p,C.c_uint];method.restype=C.c_int

    def _device(self, channel: int):
        """Verify the actual scan before buffer creation; failures never start DMA."""
        if type(channel) is not int or channel not in (1, 2) or not self.permit():
            raise ValueError('RX_CHANNEL_OR_PERMISSION')
        name=('axi-adrv9002-rx-lpc','axi-adrv9002-rx2-lpc')[channel-1]
        device=self.lib.iio_context_find_device(self.context,name.encode())
        if not device:raise RuntimeError('RX_DEVICE')
        for index in range(self.lib.iio_device_get_channels_count(device)):
            self.lib.iio_channel_disable(self.lib.iio_device_get_channel(device,index))
        channels=[self.lib.iio_device_find_channel(device,n.encode(),False) for n in ('voltage0_i','voltage0_q')]
        for index,entry in enumerate(channels):
            if not entry or self.lib.iio_channel_get_index(entry)!=index:raise RuntimeError('RX_SCAN_INDEX')
            fmt=self.lib.iio_channel_get_data_format(entry).contents
            if (fmt.length,fmt.bits,fmt.shift,fmt.is_signed,fmt.is_be,fmt.repeat)!=(16,16,0,True,False,1):
                raise RuntimeError('RX_SCAN_TYPE')
            self.lib.iio_channel_enable(entry)
        if self.lib.iio_device_get_sample_size(device)!=4 or not self.permit():raise RuntimeError('RX_SCAN_SIZE_OR_PERMISSION')
        return device,channels

    def receive(self,channel:int,seconds:int,directory:Path,barrier,strict:bool=True,block_samples:int=SAMPLES,kernel_buffers:int=4) -> dict:
        """Keep one buffer for the window; exit on layout/permission/count errors and destroy in finally.

        Write block indices, first/last/failed blocks and hashes. Byte totals describe received data, not inferred chip counters.
        The two host threads share a barrier for timing only, without claiming ADC phase synchronization.
        block_samples is 65536 or 262144; low-rate profiles must produce a block within the 750 ms timeout.
        strict=False gathers over-capacity diagnostic counts only; it does not alter acceptance or qualification.
        """
        if type(kernel_buffers) is not int or kernel_buffers not in (4,16):raise ValueError('RX_KERNEL_BUFFER_COUNT')
        if type(block_samples) is not int or block_samples not in (65536,262144):raise ValueError('RX_BLOCK_SAMPLES')
        if type(strict) is not bool:raise ValueError('RX_STRICT_MODE')
        if type(seconds) is not int or seconds not in (10,20,300):raise ValueError('RX_MEASUREMENT_SECONDS')
        directory.mkdir(mode=0o700)
        device,channels=self._device(channel)
        changed=self.lib.iio_device_set_kernel_buffers_count(device,kernel_buffers)
        if changed<0:raise OSError(-changed,'RX_KERNEL_BUFFERS_SET')
        buffer=self.lib.iio_device_create_buffer(device,block_samples,False)
        if not buffer:
            failure=C.get_errno();reset=self.lib.iio_device_set_kernel_buffers_count(device,4)
            if reset<0:raise OSError(-reset,'RX_KERNEL_BUFFERS_FAILED_CREATE_RESTORE')
            raise OSError(failure,'RX_BUFFER')
        total,blocks,within,boundary_errors,iq_errors=0,0,0,0,0
        digest=hashlib.sha256();last=None;last_raw=None;started=None;finished=None;completed=False
        queue=Queue(maxsize=4);acquired_blocks=0
        def analyse():
            """One validator accesses immutable bytes only, saves originals sequentially and never calls libiio.

            The queue is bounded and never drops blocks; futures propagate errors to RX without relaxing checks.
            """
            nonlocal total,blocks,within,boundary_errors,iq_errors,last,last_raw
            with (directory/'blocks.jsonl').open('x') as index:
                while True:
                    item=queue.get()
                    if item is None:return
                    raw,before,after=item;size=len(raw)
                    words=np.frombuffer(raw,dtype='<u2').reshape(-1,2)
                    delta=np.diff(words,axis=0)
                    inside=int(np.count_nonzero(delta!=1));different=int(np.count_nonzero(words[:,0]!=words[:,1]))
                    first=words[0].astype(np.int32);tail=words[-1].astype(np.int32)
                    boundary=int(np.count_nonzero(((first-last)&65535)!=1)) if last is not None else 0
                    entry=dict(block=blocks,bytes=size,first_iq=first.tolist(),last_iq=tail.tolist(),
                        refill_start=before,refill_end=after,within_errors=inside,boundary_errors=boundary,
                        iq_errors=different,sha256=hashlib.sha256(raw).hexdigest())
                    index.write(json.dumps(entry,separators=(',',':'))+'\n')
                    if blocks==0:(directory/'first.bin').write_bytes(raw)
                    last_raw=raw;last=tail;blocks+=1;total+=size;digest.update(raw)
                    within+=inside;boundary_errors+=boundary;iq_errors+=different
                    if inside or boundary or different:
                        if not (directory/'failure.bin').exists():(directory/'failure.bin').write_bytes(raw)
                        if strict:raise RuntimeError('RX_RAMP_OR_BOUNDARY_ERRORS')
        try:
            barrier.wait(timeout=10)
            started=time.monotonic();deadline=started+seconds
            with ThreadPoolExecutor(max_workers=1) as executor:
                analysis=executor.submit(analyse)
                try:
                    while time.monotonic()<deadline:
                        if analysis.done():analysis.result();raise RuntimeError('RX_ANALYSER_EARLY_EXIT')
                        if not self.permit():raise RuntimeError('RX_PERMISSION_BEFORE_REFILL')
                        before=time.monotonic();size=self.lib.iio_buffer_refill(buffer);after=time.monotonic()
                        if size!=block_samples*4:raise RuntimeError('RX_REFILL_LENGTH: '+str(size))
                        if not self.permit():raise RuntimeError('RX_PERMISSION_AFTER_REFILL')
                        # refill may change mappings; copy the current block only, never expose its address to the validator.
                        base=self.lib.iio_buffer_start(buffer);end=self.lib.iio_buffer_end(buffer)
                        positions=[self.lib.iio_buffer_first(buffer,c)-base for c in channels]
                        if not base or self.lib.iio_buffer_step(buffer)!=4 or positions!=[0,2] or end-base!=size:
                            raise RuntimeError('RX_BUFFER_LAYOUT')
                        raw=C.string_at(base,size)
                        try:queue.put((raw,before,after),timeout=.01)
                        except Full:
                            if analysis.done():analysis.result()
                            raise RuntimeError('RX_HOST_ANALYSIS_CAPACITY')
                        acquired_blocks+=1
                    finished=time.monotonic()
                finally:
                    stop_deadline=time.monotonic()+3
                    while not analysis.done():
                        try:queue.put(None,timeout=.05);break
                        except Full:
                            if time.monotonic()>stop_deadline:raise TimeoutError('RX_ANALYSIS_STOP_DEADLINE')
                    analysis.result(timeout=3)
            if acquired_blocks!=blocks:raise RuntimeError('RX_ANALYSIS_BLOCK_COUNT')
            completed=True
        finally:
            self.lib.iio_buffer_destroy(buffer)
            reset=self.lib.iio_device_set_kernel_buffers_count(device,4)
            if last_raw is not None:(directory/'last.bin').write_bytes(last_raw)
            finished=finished or time.monotonic()
            elapsed=finished-started if started is not None else 0
            result=dict(channel=channel,requested_seconds=seconds,completed=completed,blocks=blocks,
                samples=total//4,bytes=total,elapsed=elapsed,bytes_per_second=total/elapsed if elapsed>0 else 0,
                within_errors=within,boundary_errors=boundary_errors,iq_errors=iq_errors,
                stream_sha256=digest.hexdigest(),library_version=self.version,
                persistent_buffer=True,block_samples=block_samples,acquired_blocks=acquired_blocks,
                analysis_pipeline=True,analysis_queue_blocks=4,kernel_buffer_count=kernel_buffers,
                kernel_buffer_restored_to=4 if reset==0 else None,kernel_buffer_restore_return=reset,buffer_destroy_attempted=True,absolute_continuity_proven=False,
                counter_ambiguity_samples=65536,strict_acceptance=strict,RF_qualified=False)
            (directory/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
            if reset<0:raise OSError(-reset,'RX_KERNEL_BUFFERS_RESTORE')
        return result

# End of file: h102_iio_rx_continuous.py
