#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_host_guard.py
@brief Bounded root service retains original FT locks/quotas while USB/SDK subprocesses run unprivileged.
@details Bind exact installation/current boot/USB identity; ACK requires board STOP; independent post checks release/restores quotas.
         Input accepts no program/command/library paths; root never opens USB on behalf of users; IQ uses local C++ sockets only.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.3.0
@date 2026-10-16
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
QUOTA = Path('/sys/module/usbcore/parameters/usbfs_memory_mb')


def save(path, value):
    """Atomically write root-private receipts; control state may update, independent post saves separate results."""
    # status and guardian startup may race; unique temporary names avoid collisions between valid writers.
    temporary=path.with_name(path.name+'.tmp-'+str(os.getpid())+'-'+uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        temporary.chmod(0o600);os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def trusted(path, private=False, directory=False):
    """Root source/config stays private; public executables still require root-owned non-writable nonsymlink ancestors."""
    info=path.lstat()
    if info.st_uid!=0 or info.st_mode & (0o077 if private else 0o022) or not (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise RuntimeError('USB_HOST_UNTRUSTED_INPUT:'+str(path))
    for parent in path.parents:
        value=parent.lstat()
        if value.st_uid!=0 or value.st_mode&0o022 or not stat.S_ISDIR(value.st_mode):
            raise RuntimeError('USB_HOST_UNTRUSTED_ANCESTOR')


def deployment():
    """Recheck installation/exact C/SDK closure on every call; never load code supplied by requests."""
    trusted(ROOT,True,True);trusted(ROOT/'deployment.json',True)
    d=json.loads((ROOT/'deployment.json').read_text())
    for name,digest in d['sources'].items():
        if Path(name).name!=name:raise ValueError('USB_HOST_SOURCE_NAME')
        p=ROOT/name;trusted(p,True)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise RuntimeError('USB_HOST_SOURCE_DRIFT')
    for name,digest in d['public_files'].items():
        p=Path(d['public_root'])/name;trusted(p)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise RuntimeError('USB_HOST_BINARY_DRIFT')
    return d


def identity(d):
    """Read the sole registered device only; node changes/reboot invalidate prior sessions."""
    found=[]
    for p in Path('/sys/bus/usb/devices').iterdir():
        if (p/'idVendor').exists() and (p/'idVendor').read_text().strip()=='0403' and (
                p/'idProduct').read_text().strip()=='601f':
            found.append({k:(p/k).read_text().strip() for k in ('busnum','devnum','speed','serial')})
    if len(found)!=1 or found[0]['serial']!=d['serial'] or found[0]['speed']!='5000':raise RuntimeError('USB_HOST_IDENTITY')
    u=found[0];node='/dev/bus/usb/%03d/%03d'%(int(u['busnum']),int(u['devnum']))
    if not stat.S_ISCHR(Path(node).stat().st_mode):raise RuntimeError('USB_HOST_NODE')
    return dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),usb=u,node=node)


def unused(node):
    """Root checks all users; require empty fuser results with exit 1 before startup/quota recovery."""
    r=subprocess.run(['fuser','-v',node],capture_output=True,text=True,timeout=5)
    if r.returncode!=1 or r.stdout or r.stderr:raise RuntimeError('USB_HOST_DEVICE_OCCUPIED')


@contextmanager
def locked(d):
    """Reuse the original FT lock inode rather than competing locks; children inherit the same OFD."""
    path=Path(d['lock']);info=path.lstat();expected=tuple(d['lock_identity'])
    if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino,info.st_uid)!=expected:raise RuntimeError('USB_HOST_LOCK_DRIFT')
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    with os.fdopen(fd,'r+b') as stream:
        info=os.fstat(fd)
        if (info.st_dev,info.st_ino,info.st_uid)!=expected:raise RuntimeError('USB_HOST_LOCK_REPLACED')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield stream


def request(directory,d,*,touch=True):
    """Root requests bind installation/current hardware; never replay across nodes/installs/boots."""
    trusted(directory,True,True);trusted(directory/'request.json',True)
    r=json.loads((directory/'request.json').read_text())
    if r['host']!=identity(d) or r['deployment_sha']!=hashlib.sha256((ROOT/'deployment.json').read_bytes()).hexdigest():
        raise RuntimeError('USB_HOST_REQUEST_DRIFT')
    if touch:save(directory/'controller-heartbeat.json',dict(monotonic_ns=time.monotonic_ns()))
    return r


def run(directory,d):
    """Root retains locks until every C/SDK thread ends; no IQ reads or credentials/capabilities passed to data children."""
    r=request(directory,d)
    save(directory/'main-process.json',dict(pid=os.getpid(),stat=Path('/proc/self/stat').read_text()))
    with locked(d) as lock:
        unused(r['host']['node']);original=int(QUOTA.read_text())
        target=0 if original==0 else max(original,256)
        save(directory/'quota.json',dict(original=original,target=target))
        QUOTA.write_text(str(target))
        if int(QUOTA.read_text())!=target:raise RuntimeError('USB_HOST_QUOTA_READBACK')
        fifo=directory/'control.fifo';os.mkfifo(fifo,0o600);fd=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
        child=None
        try:
            duplex=r.get('direction')=='duplex';transmit=r.get('direction') in ('tx','duplex')
            argv=[str(Path(d['public_root'])/('logicrf-ft601-tx' if transmit else 'logicrf-ft601-rx')),d['serial'],r['socket'],str(r['seconds']),str(r['rate'])]
            if duplex:
                argv=[str(Path(d['public_root'])/'logicrf-ft601-duplex'),d['serial'],r['socket'],r['rx_socket'],
                    str(r['seconds']),str(r['rate']),str(r['rx_mask']),str(r['mask']),str(r['samples']),str(os.getpid())]
            elif transmit:argv += [str(r['mask']),str(r['samples']),str(os.getpid())]
            with (directory/'stdout').open('xb',buffering=0) as out,(directory/'stderr').open('xb',buffering=0) as err:
                child=subprocess.Popen(argv,stdin=fd,stdout=out,stderr=err,pass_fds=(lock.fileno(),),
                    user=d['uid'],group=d['gid'],extra_groups=[],env={'PATH':'/usr/bin:/bin'},cwd='/')
                save(directory/'child.json',dict(pid=child.pid,stat=Path('/proc',str(child.pid),'stat').read_text(),argv=argv))
                observed=None;start=time.monotonic()
                while child.poll() is None:
                    text=(directory/'stdout').read_text();ready='READY\n' in text
                    state=dict(ready=ready,stop_requested='STOP_REQUEST\n' in text,running=True,error='ERROR ' in text)
                    if transmit:state.update(drain_requested='DRAIN_REQUEST\n' in text,flushed='FLUSHED\n' in text,usb_done='USB_DONE\n' in text)
                    if duplex:state['rx_queued']='RX_QUEUED\n' in text
                    flags=tuple(state.values())
                    if flags!=observed:
                        if ready:
                            proof={}
                            for task in Path('/proc',str(child.pid),'task').iterdir():
                                fields=dict(line.split(':',1) for line in (task/'status').read_text().splitlines() if ':' in line)
                                row={k:fields[k].strip() for k in ('Uid','Gid','Groups','CapInh','CapPrm','CapEff','CapAmb')}
                                if ([int(v) for v in fields['Uid'].split()]!=[d['uid']]*4 or
                                        [int(v) for v in fields['Gid'].split()]!=[d['gid']]*4 or fields['Groups'].strip() or
                                        any(int(fields[k].strip(),16) for k in ('CapInh','CapPrm','CapEff','CapAmb'))):
                                    raise RuntimeError('USB_HOST_DATA_NOT_ORDINARY')
                                proof[task.name]=row
                            save(directory/'ordinary-data.json',proof)
                        save(directory/'state.json',state);observed=flags
                    heartbeat=json.loads((directory/'controller-heartbeat.json').read_text())
                    if not 0<=(time.monotonic_ns()-heartbeat['monotonic_ns'])/1e9<8:raise TimeoutError('USB_HOST_CONTROLLER_EXPIRED')
                    if r['seconds'] and time.monotonic()-start>r['seconds']+35:raise TimeoutError('USB_HOST_CHILD_DEADLINE')
                    time.sleep(.02)
                final_text=(directory/'stdout').read_text()
                final=dict(ready='READY\n' in final_text,stop_requested=True,running=False,exit=child.returncode,error=bool(child.returncode))
                if transmit:final.update(drain_requested='DRAIN_REQUEST\n' in final_text,flushed='FLUSHED\n' in final_text,usb_done='USB_DONE\n' in final_text)
                save(directory/'state.json',final)
                if child.returncode:raise RuntimeError('USB_HOST_CHILD_FAILED:'+str(child.returncode))
                save(directory/'main.json',dict(passed=True,exit=child.returncode))
                deadline=time.monotonic()+20
                while not (directory/'finish.json').exists():
                    if time.monotonic()>deadline:raise TimeoutError('USB_HOST_FINISH_CONTROLLER_LOST')
                    time.sleep(.02)
        finally:
            if child and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
            os.close(fd)


def post(directory,d):
    """Independent post verifies USB release after CGroup termination, then restores original quotas."""
    r=request(directory,d)
    save(directory/'post-process.json',dict(pid=os.getpid(),stat=Path('/proc/self/stat').read_text()))
    with locked(d):
        unused(r['host']['node']);quota=directory/'quota.json'
        if quota.exists():
            q=json.loads(quota.read_text())
            if int(QUOTA.read_text()) not in (q['original'],q['target']):raise RuntimeError('USB_HOST_FOREIGN_QUOTA')
            QUOTA.write_text(str(q['original']))
            if int(QUOTA.read_text())!=q['original']:raise RuntimeError('USB_HOST_QUOTA_RESTORE')
        save(directory/'post.json',dict(passed=True,quota=int(QUOTA.read_text()),service_result=os.environ.get('SERVICE_RESULT')))


def launch(directory,d,value):
    """Accept project-private sockets/bounded sample requests only, without arbitrary programs/libraries/users."""
    duplex=value.get('direction')=='duplex';transmit=value.get('direction') in ('tx','duplex')
    fields={'socket','seconds','rate'} | ({'direction','mask','samples'} if transmit else set())
    if duplex:fields|={'rx_socket','rx_mask'}
    if set(value)!=fields or type(value['seconds']) is not int or not 0<=value['seconds']<=300 or (
            type(value['rate']) is not int or not 192000<=value['rate']<=61440000):raise ValueError('USB_HOST_REQUEST')
    if transmit and (type(value['mask']) is not int or value['mask'] not in (1,2,3) or
            type(value['samples']) is not int or value['samples']<0 or (value['samples'] and value['samples']<16384) or value['samples']%16384 or
            ((value['seconds']==0)!=(value['samples']==0)) or (value['samples'] and value['samples']>value['rate']*(value['seconds']+1)) or value['rate']*4*(2 if value['mask']==3 else 1)>320000000):
        raise ValueError('USB_HOST_TX_REQUEST')
    if duplex and (value['mask'] not in (1,2) or type(value['rx_mask']) is not int or value['rx_mask'] not in (1,2) or value['rate']*8>320000000):
        raise ValueError('USB_HOST_DUPLEX_MODE_OR_BUDGET')
    socket=Path(value['socket']);parent=socket.parent;info=parent.lstat()
    if parent!=Path(d['socket_root']) or info.st_uid!=d['uid'] or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or (
            not re.fullmatch('[a-f0-9]{32}.sock',socket.name) or socket.exists()):raise ValueError('USB_HOST_SOCKET')
    if duplex:
        rx=Path(value['rx_socket'])
        if rx.parent!=parent or rx==socket or not re.fullmatch('[a-f0-9]{32}.sock',rx.name) or rx.exists():raise ValueError('USB_HOST_RX_SOCKET')
    directory.mkdir(mode=0o700)
    save(directory/'request.json',dict(value,host=identity(d),deployment_sha=hashlib.sha256((ROOT/'deployment.json').read_bytes()).hexdigest()))
    argv=['/usr/bin/python3','-B',str(Path(__file__).resolve()),'--session',directory.name]
    command=['systemd-run','--unit='+directory.name,'--property=Type=exec','--property=Restart=no',
        *(['--property=RuntimeMaxSec='+str(value['seconds']+45)] if value['seconds'] else []),'--property=TimeoutStopSec=8','--property=KillMode=control-group',
        '--property=UMask=0077','--property=ExecStopPost='+shlex.join(argv+['post'])]+argv+['run']
    result=subprocess.run(command,capture_output=True,text=True,timeout=10)
    save(directory/'launch.json',dict(argv=command,exit=result.returncode,stderr=result.stderr))
    if result.returncode:raise RuntimeError('USB_HOST_LAUNCH_FAILED')
    return dict(directory=str(directory),capabilities={'rx_stop_ack_v1':True,'epoch_archive_v1':True})


def status(directory,d):
    """Return originals/native-unit facts; garbage-collected state remains UNKNOWN, never assumed success."""
    request(directory,d)
    values={name:json.loads((directory/name).read_text()) if (directory/name).exists() else None for name in
        ('state.json','main.json','post.json','ordinary-data.json','main-process.json','post-process.json')}
    native=subprocess.run(['systemctl','show',directory.name+'.service','--all','--no-pager'],capture_output=True,text=True,timeout=5)
    values['native']=dict(line.split('=',1) for line in native.stdout.splitlines() if '=' in line)
    values['stdout']=(directory/'stdout').read_text() if (directory/'stdout').exists() else ''
    values['stderr']=(directory/'stderr').read_text() if (directory/'stderr').exists() else ''
    return values


def send(directory,d,value):
    """Request STOP once; ACK must match this root request's verified board STOP proof."""
    r=request(directory,d);state=json.loads((directory/'state.json').read_text())
    if not state['running']:raise RuntimeError('USB_HOST_CHILD_NOT_RUNNING')
    op=value['op']
    if r.get('direction') in ('tx','duplex'):
        byte=tx_control_byte(r,state,value)
        receipt=directory/('control-'+op+'.json')
        if receipt.exists():raise RuntimeError('USB_HOST_DUPLICATE_CONTROL')
        save(receipt,value)
        fd=os.open(directory/'control.fifo',os.O_WRONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        try:
            if os.write(fd,byte)!=1:raise RuntimeError('USB_HOST_CONTROL_WRITE')
        finally:os.close(fd)
        return dict(sent=op)
    if op=='stop_ack':
        return stop_ack(directory,state,value)
    if op=='ack':
        proof=value.get('proof',{})
        if not state['stop_requested'] or proof.get('rx_control')!=0 or proof.get('tx_control')!=0 or proof.get('state')!='STOPPED' or proof.get('ok') is not True:
            raise RuntimeError('USB_HOST_ACK_WITHOUT_STOP_PROOF')
        save(directory/'ack.json',proof)
    elif op!='stop':raise ValueError('USB_HOST_SEND_OPERATION')
    fd=os.open(directory/'control.fifo',os.O_WRONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
    try:
        if os.write(fd,b'\n' if op=='ack' else b'S')!=1:raise RuntimeError('USB_HOST_CONTROL_WRITE')
    finally:os.close(fd)
    return dict(sent=op)


def stop_ack(directory,state,value):
    """Complete the RX stop handshake in one call, retaining board STOP proof, actual STOP_REQUEST and separate ACK.

    Enter only after send checks current request/hardware and RX direction. Accept no paths, programs or
    executable text. Finite waits never substitute for helper state; TX/duplex do not use this extension.
    """
    proof=value.get('proof',{})
    if (set(value)!={'op','proof'} or proof.get('rx_control')!=0 or proof.get('tx_control')!=0 or
            proof.get('state')!='STOPPED' or proof.get('ok') is not True):
        raise RuntimeError('USB_HOST_ACK_WITHOUT_STOP_PROOF')
    if (directory/'ack.json').exists():raise RuntimeError('USB_HOST_DUPLICATE_ACK')
    started=time.monotonic()
    fd=os.open(directory/'control.fifo',os.O_WRONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
    try:
        if not state.get('stop_requested'):
            if os.write(fd,b'S')!=1:raise RuntimeError('USB_HOST_CONTROL_WRITE')
        deadline=started+2
        while True:
            state=json.loads((directory/'state.json').read_text())
            if not state.get('running') or state.get('error'):
                raise RuntimeError('USB_HOST_STOP_CHILD_FAILED')
            if state.get('stop_requested'):break
            if time.monotonic()>deadline:raise TimeoutError('USB_HOST_STOP_REQUEST_MISSING')
            time.sleep(.005)
        save(directory/'ack.json',proof)
        if os.write(fd,b'\n')!=1:raise RuntimeError('USB_HOST_CONTROL_WRITE')
    finally:os.close(fd)
    receipt=dict(sent='stop_ack',board_stop_verified=True,helper_stop_observed=True,
                 elapsed_seconds=time.monotonic()-started)
    save(directory/'stop-ack.json',receipt)
    return receipt


def tx_control_byte(request, state, value):
    """Validate pure TX control; ACK requires continuous quiet DRAIN, not CTRL0 alone."""
    op=value['op'];proof=value.get('proof')
    if op=='stop':return b'C'
    expected_rx=(0x80000000|request['rx_mask']) if op=='play' and request.get('direction')=='duplex' else 0
    if (not isinstance(proof,dict) or proof.get('ok') is not True or
            type(proof.get('rx_control')) is not int or proof['rx_control']!=expected_rx or
            type(proof.get('tx_control')) is not int):raise RuntimeError('USB_HOST_TX_PROOF')
    if op in ('drain_idle','ack'):
        tx=proof.get('tx') or {};decoded=(tx.get('snapshot') or {}).get('decoded') or {}
        flags=decoded.get('usb_flags');initial=op=='drain_idle'
        phase=(state.get('drain_requested') and not state.get('flushed')) if initial else state.get('stop_requested')
        if (not phase or proof.get('state')!=('CONFIGURED' if initial else 'STOPPED') or
                proof['tx_control']!=0x20000000 or tx.get('state')!='DRAINING' or type(flags) is not int or
                flags&0x480!=0x480 or flags&31):raise RuntimeError('USB_HOST_TX_DRAIN_PROOF')
        return b'A' if initial else b'\n'
    if op=='drain_finished':
        if not state.get('flushed') or state.get('ready') or proof.get('state')!='CONFIGURED' or proof['tx_control']:
            raise RuntimeError('USB_HOST_TX_FLUSH_PROOF')
        return b'Q'
    if op=='play':
        if (not state.get('ready') or state.get('stop_requested') or proof.get('state')!='RUNNING' or
                proof['tx_control']!=(0xc0000000|request['mask'])):raise RuntimeError('USB_HOST_TX_PLAY_PROOF')
        return b'S'
    raise ValueError('USB_HOST_TX_OPERATION')


def finish(directory,d):
    """Retain exact native-unit reference through main/post terminal state and full command/PID/Invocation receipts."""
    from h102_rf_manager_refs import _UnitRefConnection
    request(directory,d);unit=directory.name+'.service'
    with _UnitRefConnection(frozenset((unit,directory.name+'-unused.service'))) as refs:
        refs.add(unit)
        if not (directory/'main.json').exists():raise RuntimeError('USB_HOST_MAIN_INCOMPLETE')
        before=status(directory,d);native=before['native']
        pid=native['ExecMainPID'];invocation=native['InvocationID']
        if not before['main-process.json'] or pid!=str(before['main-process.json']['pid']):raise RuntimeError('USB_HOST_MAIN_PID')
        argv='/usr/bin/python3 -B '+str(Path(__file__).resolve())+' --session '+directory.name
        if 'argv[]='+argv+' run ;' not in native.get('ExecStart',''):raise RuntimeError('USB_HOST_MAIN_COMMAND')
        save(directory/'finish.json',dict(requested=True))
        deadline=time.monotonic()+15
        while True:
            result=status(directory,d);n=result['native']
            if n.get('ActiveState') not in ('active','activating','deactivating') and not n.get('Job'):break
            if time.monotonic()>deadline:raise TimeoutError('USB_HOST_NATIVE_POST_DEADLINE')
            time.sleep(.02)
        if (n.get('LoadState')!='loaded' or n.get('ExecMainPID')!=pid or n.get('InvocationID')!=invocation or
                (n.get('ExecMainCode'),n.get('ExecMainStatus'))!=('1','0') or
                'argv[]='+argv+' post ;' not in n.get('ExecStopPost','') or
                not result['post-process.json'] or not re.search(r'pid='+str(result['post-process.json']['pid'])+r' ; code=exited ; status=0',n.get('ExecStopPost','')) or
                not result['post.json'] or result['post.json']['passed'] is not True):
            raise RuntimeError('USB_HOST_NATIVE_MAIN_POST_NOT_PASS')
        save(directory/'final.json',result)
        return result


def main():
    """Entry must be the installed root-trusted script; session directories are distinct new /run names."""
    parser=argparse.ArgumentParser();parser.add_argument('--session',required=True)
    parser.add_argument('--value');parser.add_argument('action',choices=('launch','run','post','status','send','finish','evidence_open','evidence_retire'))
    args=parser.parse_args()
    if os.geteuid()!=0 or not re.fullmatch('h102-gr-usb-[a-f0-9]{32}',args.session):raise ValueError('USB_HOST_ROOT_SESSION')
    os.umask(0o077);d=deployment();directory=Path('/run')/args.session
    if args.action=='launch':result=launch(directory,d,json.loads(args.value))
    elif args.action=='run':result=run(directory,d)
    elif args.action=='post':result=post(directory,d)
    elif args.action=='finish':result=finish(directory,d)
    elif args.action=='send':result=send(directory,d,json.loads(args.value))
    elif args.action in ('evidence_open','evidence_retire'):
        import usb_host_archive
        import sys
        result=usb_host_archive.evidence(directory,d,sys.modules[__name__],
            None if args.action=='evidence_open' else json.loads(args.value))
    else:result=status(directory,d)
    if result is not None:print(json.dumps(result))

if __name__=='__main__':main()
# End of file: usb_host_guard.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
