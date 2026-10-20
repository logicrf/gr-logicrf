"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       sweep.py
@brief      Plan single-RX sweeps, linear-power stitching and bounded repeated scans.
@details    Under one native owner, stop buffers before tuning and take finite IQ; discard the first block and apply configurable settling.
            Bound latest-round/max-hold/segment identity caches. dBFS is not calibrated dBm; bands are acquired sequentially.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.8.0
@date       2026-10-14
"""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
import base64
import hashlib
import json
from pathlib import Path
from threading import Event,Lock
import time

import numpy as np

from .model import decode_iq,save_json,rates
from .session_link import SessionLink
from . import backend


@dataclass(frozen=True)
class SweepPlan:
    """Segment plan pinned to actual Fs/BW; frequencies in Hz, at most 4096 segments and 32768 output points."""
    start_hz:int
    stop_hz:int
    sample_rate_hz:int
    bandwidth_hz:int
    fft_points:int=8192
    averages:int=1
    settle_ms:int=2
    repeat:bool=False

    def validate(self):
        """Reject noninteger frequency, invalid FFT/settling and excessive segmentation."""
        if (type(self.start_hz) is not int or type(self.stop_hz) is not int
                or not 30_000_000<=self.start_hz<self.stop_hz<=6_000_000_000):raise ValueError('Sweep must span 30-6000 MHz with start below stop')
        if self.fft_points not in (1024,2048,4096,8192,16384):raise ValueError('Invalid FFT size')
        if not 1<=self.averages<=32 or not 0<=self.settle_ms<=2000:raise ValueError('Invalid average count or settling time')
        if self.sample_rate_hz<=0 or self.bandwidth_hz<=0:raise ValueError('Invalid actual Fs/bandwidth')
        if len(self.centers())>4096:raise ValueError('Sweep exceeds 4096 segments; narrow the span or use a wider profile')

    @property
    def usable_hz(self):return .8*min(self.bandwidth_hz,self.sample_rate_hz)

    def centers(self):
        """Retain the central 80% with 2% overlap; first/last edges cover the requested span exactly."""
        width=self.usable_hz;span=self.stop_hz-self.start_hz
        if width<=0:raise ValueError('Invalid actual Fs/bandwidth')
        if span<=width:return [round((self.start_hz+self.stop_hz)/2)]
        count=int(np.ceil((span-width)/(.98*width)))+1
        if count>4096:raise ValueError('Sweep exceeds 4096 segments')
        return np.rint(np.linspace(self.start_hz+width/2,self.stop_hz-width/2,count)).astype(np.int64).tolist()

    def grid(self):
        """Record FFT and output spacing separately when the output grid is coarser; original RBW is not preserved."""
        size=min(32768, int(np.ceil((self.stop_hz-self.start_hz)/(self.sample_rate_hz/self.fft_points)))+1,
                 max(2,int(np.ceil((self.stop_hz-self.start_hz)/self.usable_hz*8192))))
        return np.linspace(self.start_hz,self.stop_hz,max(2,size))


def fft_power(raw,fs,points):
    """Hann-normalized linear power retains DC; LO/DC spurs are not calibrated or subtracted."""
    values=decode_iq(raw)
    if len(values)<points:raise ValueError('IQ block is shorter than the FFT size')
    values=values[:points].astype(np.float64)
    iq=(values[:,0]+1j*values[:,1])/32768
    window=np.hanning(points)
    spectrum=np.fft.fftshift(np.fft.fft(iq*window))/window.sum()
    return np.fft.fftshift(np.fft.fftfreq(points,1/fs)),np.abs(spectrum)**2


class Accumulator:
    """Average overlapping linear power within one round; unseen regions remain NaN rather than fabricated zero dBFS."""
    def __init__(self,plan):
        self.plan=plan;self.frequency=plan.grid()
        self.power=np.zeros(len(self.frequency));self.weight=np.zeros(len(self.frequency))

    def add(self,center,offsets,power):
        """Interpolate fine output grids, take within-bin peaks for coarse grids and average overlaps in linear power.

        When output spacing exceeds FFT spacing, direct interpolation can miss narrow peaks.
        Peak reduction retains display peaks without claiming the coarse grid has original FFT resolution.
        """
        spacing=self.frequency[1]-self.frequency[0]
        if spacing>float(offsets[1]-offsets[0])*1.01:
            frequencies=center+offsets
            valid=(np.abs(offsets)<=self.plan.usable_hz/2+1)&(frequencies>=self.plan.start_hz)&(frequencies<=self.plan.stop_hz)
            indices=np.rint((frequencies[valid]-self.frequency[0])/spacing).astype(np.int64)
            bucket=np.full(len(self.frequency),-np.inf)
            np.maximum.at(bucket,indices,power[valid])
            covered=np.isfinite(bucket);self.power[covered]+=bucket[covered];self.weight[covered]+=1
            return
        mask=np.abs(self.frequency-center)<=self.plan.usable_hz/2+1
        mask&=(self.frequency>=center+offsets[0])&(self.frequency<=center+offsets[-1])
        interpolated=np.interp(self.frequency[mask],center+offsets,power)
        self.power[mask]+=interpolated;self.weight[mask]+=1

    def add_cells(self, first, power):
        """Merge board peaks on the same grid; reject bounds/negative power/unknown layouts and preserve unsampled NaNs."""
        if type(first) is not int or first<0 or first+len(power)>len(self.power):raise ValueError('SWEEP_GRID_RANGE')
        valid=np.isfinite(power)
        if np.any(power[valid]<0):raise ValueError('SWEEP_NEGATIVE_POWER')
        indices=np.arange(first,first+len(power))[valid]
        self.power[indices]+=power[valid];self.weight[indices]+=1

    def dbfs(self):
        """Return an independent bounded snapshot without shared mutable cross-thread arrays."""
        result=np.full(len(self.frequency),np.nan);valid=self.weight>0
        result[valid]=10*np.log10(np.maximum(self.power[valid]/self.weight[valid],1e-16))
        return result


class SweepSession:
    """Single-RX sweep; run is the sole hardware thread; UI calls stop/snapshot/export only.

    Each band uses finite buffers separated by tuning gaps; repetition does not provide continuous full-band IQ.
    Native ownership restores original LO. Preserve acquisition/recovery failures separately; retain latest round only, not full history.
    """
    def __init__(self,connection,info,channel,plan,output,event=None):
        plan.validate();actual=rates(info,(channel,))[channel]
        bandwidth=next(v['rf_bandwidth_hz'] for v in info['parameters']['rx'] if v['channel']==channel)
        if actual!=plan.sample_rate_hz or bandwidth!=plan.bandwidth_hz:raise ValueError('Sweep Fs/BW differs from the actual profile')
        group=next(g for g in info['radio']['groups'] if channel in g['channels'])
        if not group['editable']:raise ValueError('RX shares its LO with TX; sweep refused')
        self.connection,self.info,self.channel,self.plan,self.output=connection,info,channel,plan,output
        self.channels=(channel,);self.mode='sweep';self.pattern='normal';self.event=event or (lambda k,v:None)
        self.stopping=Event();self.lock=Lock();self.frame=None;self.last_raw=None;self.last_radio=None
        self.board=backend.board_for(connection);self.link=SessionLink(connection,info,(channel,),'sweep','normal',self.board)
        self.rounds=0;self.segments=0;self.max_hold=None;self.latest_segments=[]
        self.completed_frame=None

    def stop(self):
        """Request stop explicitly; await the current finite call/bounded tune and independent recovery."""
        self.stopping.set()

    def snapshot(self):
        """Completed arrays are immutable; retain at most one round of originals/spectra/segment identity."""
        with self.lock:return self.frame,self.completed_frame,self.rounds,self.segments

    def _publish(self,accumulator,segments,complete=False):
        spectrum=accumulator.dbfs()
        with self.lock:
            self.max_hold=spectrum.copy() if self.max_hold is None else np.fmax(self.max_hold,spectrum)
            self.frame=dict(frequency_mhz=accumulator.frequency/1e6,dbfs=spectrum,
                max_hold=self.max_hold.copy(),round=self.rounds+int(not complete),complete=complete,
                segment=len(segments),segment_count=len(self.plan.centers()),center_hz=segments[-1]['center_hz'],
                mean_segment_seconds=sum(v['total_seconds'] for v in segments)/len(segments))
            self.latest_segments=list(segments)
            if complete:self.completed_frame=self.frame

    def run(self):
        """Tune and capture finite IQ sequentially; on stop await independent post restoring original center frequency."""
        self.output.mkdir(parents=True,exist_ok=False)
        record=dict(status='RUNNING',info=self.info,plan=asdict(self.plan),channel=self.channel,RF_TX=False,
            started_at=datetime.now(timezone.utc).isoformat(),board_directory=self.link.directory,
            host_source_sha256=backend.HOST_SOURCE_SHA256,physical_rf_verified=False)
        save_json(self.output/'request.json',record)
        try:
            self.link.start();save_json(self.output/'ready.json',self.link.ready)
            self.event('ready',dict(output=str(self.output)))
            centers=self.plan.centers();native_plan=asdict(self.plan);native_plan['grid_size']=len(self.plan.grid())
            setup_started=time.monotonic();self.link.sweep_setup(native_plan)
            record['capture_setup_seconds']=time.monotonic()-setup_started
            record['execution']='board local IQ and FFT; bounded frequency cells over SSH'
            self.link.cancel_event=self.stopping;record['round_seconds']=[]
            while not self.stopping.is_set():
                round_started=time.monotonic();accumulator=Accumulator(self.plan);segments=[]
                # Keep responses within 64 KiB: one segment for narrow/high-resolution scans, at most 32 for wide panoramas.
                max_cells=int(np.ceil(self.plan.usable_hz/(self.plan.grid()[1]-self.plan.grid()[0])))+4
                batch_size=max(1,min(32,9000//max_cells))
                # Auto uses one command per segment and the 180 s firmware calibration budget; never change user policy for speed.
                if self.info['radio'].get('initial_calibrations')=='auto':batch_size=1
                for offset in range(0,len(centers),batch_size):
                    if self.stopping.is_set():break
                    reply=self.link.sweep_batch(native_plan,centers[offset:offset+batch_size])
                    for item in reply['segments']:
                        power=np.frombuffer(base64.b64decode(item.pop('power_f32le_b64'),validate=True),dtype='<f4')
                        accumulator.add_cells(item['grid_first'],power);self.segments+=1
                        item.update(host_received_at=datetime.now(timezone.utc).isoformat())
                        segments.append(item)
                    if segments:self._publish(accumulator,segments)
                    with self.lock:self.last_radio=reply['radio']
                    if reply['stopped']:break
                complete=len(segments)==len(centers)
                if complete and not np.isfinite(accumulator.dbfs()).all():raise RuntimeError('SWEEP_INCOMPLETE_GRID')
                if complete:
                    self.rounds+=1;record['round_seconds'].append(time.monotonic()-round_started)
                if segments:self._publish(accumulator,segments,complete)
                if not complete or not self.plan.repeat:break
            record.update(status='PASS_SWEEP_SESSION',stopped_by_user=self.stopping.is_set(),rounds=self.rounds,segments=self.segments)
        except Exception as error:record.update(status='FAIL_SWEEP_SESSION',error=str(error))
        finally:
            try:
                manager=self.link.close();native=backend.collect_session(self.board,self.link.directory,self.info['boot_id'])
                save_json(self.output/'control-end.json',dict(manager=manager,error=str(self.link.error) if self.link.error else None))
                save_json(self.output/'board-records.json',native);post=native.get('post.json',{})
                record['recovery_confirmed']=bool(post.get('passed') and post.get('configuration_unchanged') and post.get('settings_restored'))
                if not record['recovery_confirmed'] or not manager or manager['returncode']:
                    record.update(status='FAIL_SWEEP_SESSION',recovery_error='Native exit or independent recovery is unconfirmed')
            except Exception as error:record.update(status='FAIL_SWEEP_SESSION',recovery_confirmed=False,recovery_error=str(error))
            if self.frame:
                try:self.export(self.output/'latest')
                except Exception as error:record.update(status='FAIL_SWEEP_SESSION',export_error=str(error))
            self.board.password=None;record['finished_at']=datetime.now(timezone.utc).isoformat();save_json(self.output/'result.json',record)
        self.event('finished',record);return record

    def export(self,directory):
        """Save the latest complete/partial round to a new directory with actual segment times and unsampled NaNs."""
        with self.lock:
            if self.frame is None:raise ValueError('No sweep data available yet')
            frame=self.frame;segments=list(self.latest_segments)
        directory.mkdir(parents=True,exist_ok=False)
        path=directory/'spectrum.npz'
        np.savez_compressed(path,frequency_hz=frame['frequency_mhz']*1e6,dbfs=frame['dbfs'],max_hold_dbfs=frame['max_hold'])
        metadata=dict(info=self.info,plan=asdict(self.plan),channel=self.channel,rounds=self.rounds,segments=segments,
            complete=frame['complete'],source='normal',unit='dBFS_not_calibrated_dBm',physical_rf_verified=False,
            fft_bin_hz=self.plan.sample_rate_hz/self.plan.fft_points,
            output_grid_hz=float(np.diff(frame['frequency_mhz'][:2])[0]*1e6),
            coarse_grid_aggregation='peak per output cell; overlap averaged in linear power',
            historical_rounds_retained=False,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        save_json(directory/'sweep.json',metadata);return metadata


# End of file: sweep.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
