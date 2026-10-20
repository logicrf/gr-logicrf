#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file fft_recording.py
@brief Process completed cf32 recordings with native GNU Radio FFT into bounded spectral tables and optional PNG.
@details File Source to Head to Stream to Vector to FFT to Magnitude Squared runs entirely
         in C++; Python handles bounded spectrum frames and recording metadata, without claiming live RF calibration.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from gnuradio import blocks,fft,gr


def analyze(path,rate,center=0,size=4096,frames=16):
    """Process the first frames nonoverlapping windowed frames with dBFS peaks and bounded FFT-frame memory.

    Require cf32_le and at least one full frame; use shifted FFT and window-amplitude normalization without removing DC.
    """
    if size<256 or size>65536 or size&(size-1) or not 1<=frames<=256 or rate<=0:raise ValueError('FFT_GEOMETRY')
    count=min(frames,Path(path).stat().st_size//(8*size))
    if count<1:raise ValueError('FFT_NO_COMPLETE_FRAME')
    window=np.hanning(size);graph=gr.top_block()
    source=blocks.file_source(gr.sizeof_gr_complex,str(path),False)
    head=blocks.head(gr.sizeof_gr_complex,count*size)
    vectors=blocks.stream_to_vector(gr.sizeof_gr_complex,size)
    transform=fft.fft_vcc(size,True,window.tolist(),True,1)
    power=blocks.complex_to_mag_squared(size);sink=blocks.vector_sink_f(size)
    graph.connect(source,head,vectors,transform,power,sink);graph.run();source.close()
    values=np.asarray(sink.data(),dtype=np.float64).reshape(-1,size)
    if values.shape!=(count,size):raise RuntimeError('FFT_FRAME_COUNT')
    db=10*np.log10(np.maximum(values.mean(axis=0)/(window.sum()**2),1e-40))
    hz=np.fft.fftshift(np.fft.fftfreq(size,1/rate));peak=int(np.argmax(db))
    return dict(frames=count,fft_size=size,rate_hz=rate,center_hz=center,
        peak_offset_hz=float(hz[peak]),peak_rf_hz=float(center+hz[peak]),peak_dbfs=float(db[peak])),hz,db


def main():
    """Require PASS recording metadata; always produce CSV and use matplotlib only for explicitly requested PNG."""
    parser=argparse.ArgumentParser();parser.add_argument('recording',type=Path)
    parser.add_argument('--channel',type=int,choices=(1,2),default=1)
    parser.add_argument('--fft-size',type=int,default=4096);parser.add_argument('--frames',type=int,default=16)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--png',action='store_true')
    args=parser.parse_args();record=json.loads((args.recording/'recording.json').read_text())
    if not record['status'].startswith('PASS') or str(args.channel) not in record['files']:raise ValueError('COMPLETED_RECORDING_REQUIRED')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    result,hz,db=analyze(args.recording/f'rx{args.channel}.cf32',record['rate_hz'],record['center_frequency_hz'],args.fft_size,args.frames)
    with (args.output/'spectrum.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['offset_hz','rf_hz','dbfs'])
        writer.writerows(zip(hz,hz+result['center_hz'],db))
    if args.png:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        figure,axis=plt.subplots(figsize=(10,4));axis.plot((hz+result['center_hz'])/1e6,db,linewidth=.8)
        axis.set(xlabel='RF frequency (MHz)',ylabel='dBFS',title='H102 recorded IQ');axis.grid(alpha=.2)
        figure.tight_layout();figure.savefig(args.output/'spectrum.png',dpi=140);plt.close(figure)
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
# End of file: fft_recording.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
