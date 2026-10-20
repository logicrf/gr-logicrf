"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file runtime_archive.py
@brief Archive completed owned board USB evidence, verify actual restoration, then reclaim exact /run paths.
@details Accept only this run's two private directories, same boot, idle locks and terminal child services. Retain full file
         hashes/raw tar; archive FIFO metadata only without restoring active endpoints on host. Preserve originals on failure.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile

# Run only in verified short root SSH transactions; never import code from directories being retired.
ROOT_CODE=r'''
import base64,fcntl,hashlib,io,json,os,re,shutil,stat,subprocess,tarfile,time
from pathlib import Path
p=PARAMETERS
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==p['boot_id']
assert Path('/etc/machine-id').read_text().strip()==p['machine_id']
paths=[Path(raw) for raw in p['paths']]
assert len(paths)==2 and re.fullmatch('/run/h102-runtime-usb-data-c16-gr-[0-9a-f]{32}',str(paths[0]))
assert re.fullmatch('/run/h102-gr-usb-code-[0-9a-f]{32}',str(paths[1]))
locks=[];deadline=time.monotonic()+10
for name in ('h102-profile-config.lock','h102-rx-session.lock','h102-gnss-g0.lock'):
 f=open('/run/lock/'+name,'r+')
 while True:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
  except BlockingIOError:
   if time.monotonic()>deadline:raise TimeoutError('USB_ARCHIVE_RECOVERY_LOCKS_NOT_RELEASED')
   time.sleep(.02)
 locks.append(f)
assert json.loads((paths[0]/'outer-post.json').read_text())['status']=='OUTER_POST_PASS'
assert json.loads((paths[0]/'post.json').read_text())['status']=='POST_PASS'
assert json.loads((paths[0]/'summary.json').read_text())['status']=='PASS_DIGITAL_RX_MAIN_POST'
for unit in (paths[0].name+'.service',paths[0].name+'-manager.service'):
 while True:
  state=subprocess.run(['systemctl','show',unit,'-p','ActiveState','--value'],capture_output=True,text=True,check=True,timeout=5).stdout.strip()
  if state in ('inactive','failed'):break
  if time.monotonic()>deadline:raise TimeoutError('USB_ARCHIVE_UNIT_NOT_TERMINAL:'+unit+':'+state)
  time.sleep(.02)
manifest={};total=0
for root in paths:
 for path in [root,*sorted(root.rglob('*'))]:
  info=path.lstat();assert info.st_uid==0 and not info.st_mode&0o022 and not stat.S_ISLNK(info.st_mode)
  assert stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISFIFO(info.st_mode)
  if stat.S_ISREG(info.st_mode):
   total+=info.st_size;assert total<=64*1024*1024 and len(manifest)<4096
   manifest[str(path)]=dict(bytes=info.st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),inode=info.st_ino,mtime_ns=info.st_mtime_ns)
if p.get('expected') is not None:
 assert manifest==p['expected']
 for root in paths:shutil.rmtree(root)
 print(json.dumps(dict(status='RETIRED_EXACT_VERIFIED',paths=p['paths'],files=len(manifest),bytes=total,
  available_bytes=os.statvfs('/run').f_bavail*os.statvfs('/run').f_frsize)))
else:
 buffer=io.BytesIO()
 with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
  raw=json.dumps(manifest,sort_keys=True).encode();item=tarfile.TarInfo('manifest.json');item.size=len(raw);archive.addfile(item,io.BytesIO(raw))
  for root in paths:archive.add(root,arcname=str(root).lstrip('/'),recursive=True)
 raw=buffer.getvalue();assert len(raw)<=8*1024*1024
 print(json.dumps(dict(archive_base64=base64.b64encode(raw).decode(),archive_sha256=hashlib.sha256(raw).hexdigest())))
'''


def retire(controller):
    """After independent recovery, archive/restore-check all files, recheck originals and delete exact owned paths.

    Archive/restore failures raise while preserving board originals; never scan unrelated history.
    """
    from h102_control.client import Client
    client=Client(controller.connection)
    identifier=controller.api_session_id
    description=client.request('usb.evidence.open',dict(session_id=identifier),timeout=75)
    size=description['bytes'];chunk=description['chunk_size']
    if type(size) is not int or not 0<size<=8*1024*1024 or chunk!=65536:raise RuntimeError('USB_ARCHIVE_GEOMETRY')
    pieces=[]
    for index in range((size+chunk-1)//chunk):
        item=client.request('usb.evidence.chunk',dict(session_id=identifier,index=index))
        if item['index']!=index:raise RuntimeError('USB_ARCHIVE_CHUNK_IDENTITY')
        pieces.append(base64.b64decode(item['data'],validate=True))
    raw=b''.join(pieces)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=description['archive_sha256']:raise RuntimeError('USB_ARCHIVE_TRANSFER_SHA')
    output=controller.run_output/'runtime-archive';output.mkdir(mode=0o700)
    from .epoch_evidence import durable, json_file
    import os
    durable(output/'runtime.tar.gz',raw);restored=output/'restored';restored.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        members=archive.getmembers()
        for member in members:
            if member.name.startswith('/') or '..' in Path(member.name).parts or not(member.isfile() or member.isdir() or member.isfifo()):
                raise RuntimeError('USB_ARCHIVE_MEMBER')
        archive.extractall(restored,members=[m for m in members if m.isfile() or m.isdir()])
    manifest=json.loads((restored/'manifest.json').read_text())
    for path,entry in manifest.items():
        file=restored/path.lstrip('/')
        if file.stat().st_size!=entry['bytes'] or hashlib.sha256(file.read_bytes()).hexdigest()!=entry['sha256']:
            raise RuntimeError('USB_ARCHIVE_RESTORE_SHA')
        fd=os.open(file,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    for directory in [*sorted((p for p in restored.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True),restored,output]:
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    json_file(controller.run_output/'runtime-archive-verified.json',dict(archive_sha256=description['archive_sha256'],files=len(manifest),restore_verified=True,fsync_verified=True))
    receipt=client.request('usb.evidence.retire',dict(session_id=identifier,archive_sha256=description['archive_sha256']),timeout=75)
    controller._save('runtime-retire.json',receipt)

# End of file: runtime_archive.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
