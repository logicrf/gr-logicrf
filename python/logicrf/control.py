"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file control.py
@brief H102 state, static profile catalog and independent PMT control block.
@details Control stays outside the IQ path; a bounded serial queue separates stopped plans from active readback.
         PMT replies use result_json to preserve 64-bit counts/signatures without float conversion.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
from copy import deepcopy
import json
from pathlib import Path
from queue import Queue,Empty,Full
from threading import Event,Thread
import pmt
from gnuradio import gr


def configuration_catalog():
    """Return 58 declared 1.2.0 profiles offline; never replace startup's actual Fs/BW/SHA checks."""
    return json.loads((Path(__file__).parent/'locks/h102-profiles.json').read_text())


def device_status(board):
    """Return plans, counts, latest runtime readback and recovery without connecting or reading credentials.

    last_active_info describes the latest session, not current board settings after exit; independent post determines recovery.
    """
    options=board.options();controller=getattr(board,'controller',None)
    tx=getattr(board,'tx_native',getattr(board,'native',board) if options.direction=='tx' else None)
    rx=getattr(board,'rx_native',getattr(board,'native',board) if options.direction=='rx' else None)
    active=bool(controller and (getattr(controller,'link',None) or getattr(controller,'ready',None)))
    plan=dict(backend=options.backend,configuration=options.configuration,sample_rate_hz=options.sample_rate,
        center_frequency_hz=options.center_frequency,tx_samples=options.tx_samples,sample_limit=options.sample_limit,
        frame_samples=options.frame_samples,queue_frames=options.queue_frames)
    if controller:
        for key in ('rx_channels','tx_channels','rf_enabled','attenuation_db','rx_gain_db'):
            if hasattr(controller,key):plan[key]=deepcopy(getattr(controller,key))
    return dict(schema=1,plan=plan,control_errors=list(getattr(controller,'finish_errors',[])),error=board.error(),ready=board.ready(),session_active=active,
        last_active_info=deepcopy(getattr(controller,'info',None)),
        last_active_info_is_current=active and not board.error(),
        recovery_confirmed=bool(controller and controller.recovery_confirmed),
        tx={str(ch):tx.tx_stats(ch) for ch in (1,2)} if tx else {},
        rx={str(ch):dict(received_samples=rx.received(ch),queue=rx.queue_stats(ch)) for ch in (1,2)} if rx else {})


def select_configuration(board,profile):
    """Select an exact profile while stopped and update planned Fs; applications must update other DSP block rates."""
    entries={p['id']:p for p in configuration_catalog()['profiles']}
    if profile not in entries:raise ValueError('UNKNOWN_CONFIGURATION_ID')
    rate=entries[profile]['parameters']['rx'][0]['sample_rate_hz']
    from .logicrf_python import device
    if isinstance(board,device):board.configure(profile,rate,board.options().center_frequency)
    else:board.configure(profile,rate)
    return dict(profile=profile,sample_rate_hz=rate,hardware_applied=False,apply_at_next_start=True)


def dispatch(board,request):
    """Execute explicit control without starting/reconnecting graphs or claiming deferred plans have reached hardware."""
    if not isinstance(request,dict) or not isinstance(request.get('action'),str):raise ValueError('CONTROL_ACTION_REQUIRED')
    action=request['action'];fields={
        'status':set(),'profiles':set(),'select_profile':{'profile'},'tune':{'direction','frequency_hz'},
        'rx_gain':{'channel','gain_db'},'tx_attenuation':{'attenuation_db'},
        'stop_rx':set(),'stop_tx':set(),'abort':set()}
    if action not in fields:raise ValueError('UNKNOWN_CONTROL_ACTION')
    if set(request)-{'id','action'}!=fields[action]:raise ValueError('CONTROL_FIELDS')
    if action=='status':return device_status(board)
    if action=='profiles':return configuration_catalog()
    if action=='select_profile':return select_configuration(board,request['profile'])
    if action=='tune':
        direction=request['direction']
        if direction not in ('rx','tx'):raise ValueError('CONTROL_TUNE_DIRECTION')
        controller=getattr(board,'controller',None)
        if controller and getattr(controller,'prepared_options',None) is not None:raise RuntimeError('STOP_REQUIRED_FOR_TUNE')
        native=getattr(board,direction+'_native',getattr(board,'native',board))
        if native is None or native.options().direction!=direction:raise ValueError('CONTROL_DIRECTION_NOT_PRESENT')
        native.set_center_frequency(request['frequency_hz'])
        return dict(direction=direction,frequency_hz=request['frequency_hz'],hardware_applied=False,apply_at_next_start=True)
    if action=='rx_gain':
        board.set_rx_gain(request['channel'],request['gain_db']);return dict(hardware_applied=False,apply_at_next_start=True)
    if action=='tx_attenuation':
        board.set_tx_attenuation(request['attenuation_db']);return dict(hardware_applied=False,apply_at_next_start=True)
    if action=='stop_rx':
        if hasattr(board,'stop_rx'):board.stop_rx()
        else:board.request_rx_stop()
        return dict(stop_requested=True,drain_with_graph_wait=True)
    if action=='stop_tx':
        target=board.stop_tx() if hasattr(board,'stop_tx') else board.request_tx_stop()
        return dict(target_samples=target,drain_with_graph_wait=True)
    board.shutdown();return dict(aborted=True,error=board.error())


class h102_control(gr.basic_block):
    """command input/events output; callers must retain this Python object's strong reference.

    Queue at most eight requests; start/stop manages this worker only. Source/Sink lifecycle owns hardware sessions.
    Reply with status/action/id/result_json or error; drain normal data before stopping the graph.
    """
    def __init__(self,board):
        gr.basic_block.__init__(self,name='h102_control',in_sig=None,out_sig=None)
        self.board=board;self.queue=Queue(maxsize=8);self.done=Event();self.worker=None
        self.message_port_register_in(pmt.intern('command'));self.message_port_register_out(pmt.intern('events'))
        self.set_msg_handler(pmt.intern('command'),self._receive)

    def _emit(self,request,result=None,error=None):
        """Preserve request correlation; result_json integers retain all original 64-bit values exactly."""
        event=dict(status='error' if error else 'ok',action=str(request.get('action','')),id=str(request.get('id','')))
        if error:event['error']=str(error)
        else:event['result_json']=json.dumps(result,separators=(',',':'),allow_nan=False)
        self.message_port_pub(pmt.intern('events'),pmt.to_pmt(event))

    def _receive(self,message):
        """Scheduler messages enqueue only; full queues/stopped state produce immediate explicit errors."""
        try:request=pmt.to_python(message)
        except Exception:self._emit({},error='CONTROL_DICT_REQUIRED');return
        if not isinstance(request,dict):self._emit({},error='CONTROL_DICT_REQUIRED');return
        if self.worker is None or self.done.is_set():self._emit(request,error='CONTROL_NOT_RUNNING');return
        try:self.queue.put_nowait(request)
        except Full:self._emit(request,error='CONTROL_QUEUE_FULL')

    def _run(self):
        """Serialize low-rate control without IQ access; one exception never hides subsequent error replies."""
        while not self.done.is_set():
            try:request=self.queue.get(timeout=.1)
            except Empty:continue
            try:self._emit(request,dispatch(self.board,request))
            except Exception as error:self._emit(request,error=error)

    def start(self):
        self.done.clear();self.worker=Thread(target=self._run,name='h102-gr-control',daemon=True);self.worker.start();return True

    def stop(self):
        self.done.set()
        if self.worker:
            self.worker.join(30)
            if self.worker.is_alive():raise RuntimeError('CONTROL_WORKER_DID_NOT_STOP')
            self.worker=None
        while True:
            try:self._emit(self.queue.get_nowait(),error='CONTROL_STOPPED')
            except Empty:break
        return True

# End of file: control.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
