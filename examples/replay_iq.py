#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file replay_iq.py
@brief Verify completed cf32 recordings and replay GNU spectrum/waterfall without a device connection.
@details Verify full SHA before creating windows; C++ File Source/Throttle handles data.
         Sample rate/center frequency come from the same recording; read-only files, no RF transmission.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.0
@date 2026-10-17
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from threading import Thread


def main():
    """Accept completed recordings only; stop/join C++ graph after window close; no Linux credentials."""
    parser=argparse.ArgumentParser();parser.add_argument('recording',type=Path)
    parser.add_argument('--channel',type=int,choices=(1,2),default=1);parser.add_argument('--seconds',type=float,default=0)
    args=parser.parse_args()
    if args.seconds<0:parser.error('seconds >= 0')
    record=json.loads((args.recording/'recording.json').read_text())
    if not record['status'].startswith('PASS'):raise ValueError('COMPLETED_RECORDING_REQUIRED')
    item=record['files'][str(args.channel)];path=args.recording/f'rx{args.channel}.cf32'
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1<<20),b''):digest.update(chunk)
    if digest.hexdigest()!=item['sha256']:raise ValueError('RECORDING_SHA_MISMATCH')
    from PyQt5 import QtCore,QtWidgets,sip
    from gnuradio import blocks,fft,gr,qtgui
    app=QtWidgets.QApplication(sys.argv[:1]);widget=QtWidgets.QWidget();widget.setWindowTitle('H102 IQ replay · No RF transmission');widget.resize(1100,780)
    layout=QtWidgets.QVBoxLayout(widget);rate=record['rate_hz'];center=record['center_frequency_hz']
    layout.addWidget(QtWidgets.QLabel(f'RX{args.channel} · {rate} SPS · {center/1e6:.6f} MHz · cf32 LE'))
    spectrum=qtgui.freq_sink_c(4096,fft.window.WIN_HANN,center,rate,'Recorded IQ spectrum',1)
    waterfall=qtgui.waterfall_sink_c(1024,fft.window.WIN_HANN,center,rate,'Recorded IQ waterfall',1)
    for plot in (spectrum,waterfall):layout.addWidget(sip.wrapinstance(plot.qwidget(),QtWidgets.QWidget))
    graph=gr.top_block();source=blocks.file_source(gr.sizeof_gr_complex,str(path),True)
    throttle=blocks.throttle(gr.sizeof_gr_complex,rate,True);graph.connect(source,throttle,spectrum);graph.connect(throttle,waterfall)
    graph.start();waiter=Thread(target=graph.wait);waiter.start();widget.show()
    if args.seconds:QtCore.QTimer.singleShot(int(args.seconds*1000),widget.close)
    try:app.exec_()
    finally:graph.stop();waiter.join(10);source.close()
    if waiter.is_alive():raise RuntimeError('REPLAY_GRAPH_NOT_JOINED')
    print(json.dumps(dict(status='PASS_REPLAY',channel=args.channel,rate_hz=rate,center_hz=center,RF_TX=False)))
    return 0

if __name__=='__main__':raise SystemExit(main())
# End of file: replay_iq.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
