"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_pair_relay_model.py
@brief Check paired API relay READY/START admission through actual sockets/subprocesses.
@details Import API from independently generated packages; the data subprocess echoes one control line without hardware.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace

sys.path.insert(0,str(Path(sys.argv[1]).resolve()))
from h102_control.protocol import encode_frame,receive_frame,ProtocolError
from h102_control.server import ApiHandler


def exercise(kind, reusable, allowed, operation='receiver_ready_start'):
    """Relay actual USB reusable commands only; reject other kinds/unknown actions before child stdin."""
    left,right=socket.socketpair();right.settimeout(3);errors=[]
    process=subprocess.Popen([sys.executable,'-B','-c','import sys; print(sys.stdin.readline(),end="",flush=True)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    handler=object.__new__(ApiHandler);handler.request=left
    session=SimpleNamespace(process=process,kind=kind,public_info={'reusable':reusable})
    def relay():
        try:handler._relay(session)
        except BaseException as error:errors.append(error)
    worker=Thread(target=relay);worker.start()
    control=dict(op=operation,token='model',seq=1,stream_id='41',configuration_sha256='a'*64)
    try:
        right.sendall(encode_frame(dict(control=control)))
        if allowed:
            with right.makefile('rb',buffering=0) as reader:
                reply=receive_frame(reader);assert reply['stream']=='stdout'
                assert json.loads(base64.b64decode(reply['data']))==control
                assert receive_frame(reader)==dict(stream='exit',status=0)
            worker.join(3);assert not errors and not worker.is_alive()
        else:
            worker.join(3);assert not worker.is_alive()
            assert len(errors)==1 and isinstance(errors[0],ProtocolError) and str(errors[0])=='CONTROL_API_SESSION_ACTION'
            process.stdin.close();process.wait(timeout=3)
            assert process.stdout.read()==b'', 'rejected command reached child'
    finally:
        left.close();right.close()
        if process.poll() is None:process.kill();process.wait(timeout=3)
        worker.join(3)
        for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()


exercise('usb',True,True)
exercise('usb',False,False)
exercise('iio',True,False)
exercise('usb',True,False,'unknown_operation')
print(json.dumps(dict(status='PASS_REAL_RELAY_MODEL_ONLY',cases=4,hardware=False)))

# End of file: usb_pair_relay_model.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
