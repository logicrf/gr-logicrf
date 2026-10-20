#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_host_broker.py
@brief Local UNIX interface to a fixed USB guard for one installed user without everyday sudo prompts.
@details SO_PEERCRED binds the installation UID; accept launch/status/send/finish and exact session IDs only.
         Reject program paths/commands; generate no heartbeat; preserve FT lock, quotas and independent post.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.0
@date 2026-10-18
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from h102_control.protocol import encode_frame, receive_frame


class Server(socketserver.ThreadingUnixStreamServer):
    """Same installed UID only; at most eight bounded calls; verify root source hashes at startup."""
    daemon_threads=True
    def __init__(self,address,guard,uid,*,inline_observation=False):
        self.guard,self.uid=guard,uid
        self.observation_guard=None
        if inline_observation:
            # Reuse administrator-pinned guard only, never client program paths; code/deployment stays frozen during
            # module lifetime; each observation still checks hashes and the complete installed closure.
            self.guard_sha=hashlib.sha256(guard.read_bytes()).hexdigest()
            self.deployment_path=guard.parent/'deployment.json'
            self.deployment_sha=hashlib.sha256(self.deployment_path.read_bytes()).hexdigest()
            spec=importlib.util.spec_from_file_location('h102_fixed_observation_guard',guard)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            module.deployment()  # Validate the full actual installed closure.
            self.observation_guard=module
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(address,Handler)
    def observe(self,action,session,payload):
        """Reuse bounded status/send only; deployment rehashes actual files for every request.

        status retains actual systemd/USB reads; send retains STOP/ACK proofs/deadlines.
        launch/finish/archive use isolated subprocesses with the original 30-second outer deadline.
        """
        if (action not in ('status','send') or self.observation_guard is None or
                hashlib.sha256(self.guard.read_bytes()).hexdigest()!=self.guard_sha or
                hashlib.sha256(self.deployment_path.read_bytes()).hexdigest()!=self.deployment_sha):
            raise RuntimeError('USB_HOST_BROKER_OBSERVATION_PIN')
        module=self.observation_guard;deployment=module.deployment()
        directory=Path('/run')/session
        return module.status(directory,deployment) if action=='status' else module.send(directory,deployment,payload)

    def process_request(self,request,address):
        if not self.slots.acquire(False):request.close();return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class Handler(socketserver.BaseRequestHandler):
    """Reject other UIDs, arbitrary actions/paths and extra fields; guard rechecks the entire fixed installation."""
    def handle(self):
        self.request.settimeout(8)
        try:
            pid,uid,gid=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=self.server.uid:raise PermissionError('USB_HOST_BROKER_UID')
            with self.request.makefile('rb') as reader:value=receive_frame(reader)
            if set(value)!={'action','session','value'} or value['action'] not in ('launch','status','send','finish','evidence_open','evidence_retire'):
                raise ValueError('USB_HOST_BROKER_OPERATION')
            if not isinstance(value['session'],str) or not re.fullmatch('h102-gr-usb-[a-f0-9]{32}',value['session']):
                raise ValueError('USB_HOST_BROKER_SESSION')
            action=value['action'];payload=value['value']
            if action in ('status','finish','evidence_open') and payload is not None:raise ValueError('USB_HOST_BROKER_EXTRA_VALUE')
            if action in ('launch','send','evidence_retire') and not isinstance(payload,dict):raise ValueError('USB_HOST_BROKER_VALUE')
            argv=['/usr/bin/python3','-B',str(self.server.guard),'--session',value['session']]
            if payload is not None:argv+=['--value',json.dumps(payload,separators=(',',':'),allow_nan=False)]
            if self.server.observation_guard is not None and action in ('status','send'):
                response=dict(ok=True,result=self.server.observe(action,value['session'],payload))
            else:
                result=subprocess.run(argv+[action],stdin=subprocess.DEVNULL,capture_output=True,text=True,
                                      timeout=30,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','PYTHONNOUSERSITE':'1'})
                if result.returncode:raise RuntimeError(result.stderr[-1800:])
                response=dict(ok=True,result=json.loads(result.stdout))
        except Exception as error:response=dict(ok=False,error=str(error)[-2000:])
        try:self.request.sendall(encode_frame(response))
        except OSError:pass


def main():
    """Only administrator deployment selects the fixed guard path; ordinary clients cannot change it."""
    parser=argparse.ArgumentParser();parser.add_argument('--guard',type=Path,required=True)
    parser.add_argument('--guard-sha256',required=True);parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--inline-observation',action='store_true');parser.add_argument('--socket',type=Path,default=Path('/run/logicrf-h102-usb/control.sock'));args=parser.parse_args()
    if os.geteuid()!=0:raise PermissionError('USB_HOST_BROKER_ROOT_SERVICE')
    path=args.guard
    for parent in (path.parent,*path.parent.parents):
        info=parent.lstat()
        if info.st_uid!=0 or info.st_mode&0o022:raise PermissionError('USB_HOST_BROKER_GUARD_ANCESTOR')
    if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022 or hashlib.sha256(path.read_bytes()).hexdigest()!=args.guard_sha256:
        raise PermissionError('USB_HOST_BROKER_GUARD_PIN')
    os.umask(0o077);args.socket.parent.mkdir(mode=0o755,exist_ok=True)
    # PID1 exclusively owns /run service directory; remove this socket's stale entry only, rejecting regular files/links.
    if args.socket.exists():
        import stat
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):raise PermissionError('USB_HOST_BROKER_SOCKET_OCCUPIED')
        args.socket.unlink()
    with Server(str(args.socket),path,args.uid,inline_observation=args.inline_observation) as server:
        os.chown(args.socket,args.uid,-1);args.socket.chmod(0o600)
        server.serve_forever()

if __name__=='__main__':main()
# End of file: usb_host_broker.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
