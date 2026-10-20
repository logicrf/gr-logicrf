"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_runtime_archive.py
@brief Fully archive, restore-verify and precisely retire completed owned IIO directories.
@details Fixed board code rejects network paths; hold three locks and check boot, independent post and systemd terminal state.
         Host retains raw tar/restored files and issues SHA receipts after fsync; failures preserve originals.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

# Server-only fixed arguments; both phases recheck identity/locks/terminal state and every tree entry.
ROOT_CODE = r'''
import fcntl,hashlib,io,json,os,re,shutil,stat,subprocess,tarfile,time
from pathlib import Path
p=PARAMETERS
root=Path(p['directory'])
assert re.fullmatch('/run/h102-gui-session-[0-9a-f]{32}',str(root))
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==p['boot_id']
assert Path('/etc/machine-id').read_text().strip()==p['machine_id']
locks=[];deadline=time.monotonic()+5
for name in ('h102-profile-config.lock','h102-rx-session.lock','h102-gnss-g0.lock'):
 f=open('/run/lock/'+name,'r+')
 while True:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
  except BlockingIOError:
   if time.monotonic()>=deadline:raise TimeoutError('RX_ARCHIVE_RECOVERY_LOCKS_NOT_RELEASED')
   time.sleep(.02)
 locks.append(f)
request=json.loads((root/'request.json').read_text())
assert request['identity']['boot_id']==p['boot_id'] and request['identity']['machine_id']==p['machine_id']
post=json.loads((root/'post.json').read_text())
assert all(post.get(k) is True for k in ('passed','settings_restored','configuration_unchanged'))
assert json.loads((root/'manager.json').read_text())['returncode']==0
unit=request['unit'];assert re.fullmatch(r'h102-gr-iio-stream-[0-9a-f]{32}\.service',unit)
while True:
 state=subprocess.run(['systemctl','show',unit,'-p','ActiveState','--value'],capture_output=True,text=True,check=True,timeout=5).stdout.strip()
 if state in ('inactive','failed'):break
 if time.monotonic()>=deadline:raise TimeoutError('RX_ARCHIVE_UNIT_NOT_TERMINAL:'+state)
 time.sleep(.02)
manifest={};total=0
for path in [root,*sorted(root.rglob('*'))]:
 info=path.lstat();assert info.st_uid==0 and not info.st_mode&0o022
 assert stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
 entry=dict(kind='directory' if stat.S_ISDIR(info.st_mode) else 'file',mode=stat.S_IMODE(info.st_mode),
            inode=info.st_ino,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)
 if entry['kind']=='file':
  total+=info.st_size;assert total<=8*1024*1024
  entry.update(bytes=info.st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
 manifest[str(path)]=entry;assert len(manifest)<=4096
# systemd terminal state/worker poll excludes active descendants; no other process may retain directory/files.
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit():continue
 for handle in [proc/'cwd',proc/'root',*list((proc/'fd').glob('*'))]:
  try:target=os.readlink(handle)
  except (FileNotFoundError,PermissionError,OSError):continue
  assert target!=str(root) and not target.startswith(str(root)+'/')
if p.get('expected') is not None:
 assert manifest==p['expected']
 shutil.rmtree(root)
 print(json.dumps(dict(status='RETIRED_EXACT_VERIFIED',directory=str(root),entries=len(manifest),bytes=total,
                      available_bytes=os.statvfs('/run').f_bavail*os.statvfs('/run').f_frsize)))
else:
 import base64
 buffer=io.BytesIO()
 with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
  raw=json.dumps(manifest,sort_keys=True).encode();item=tarfile.TarInfo('manifest.json');item.size=len(raw)
  archive.addfile(item,io.BytesIO(raw));archive.add(root,arcname=str(root).lstrip('/'),recursive=True)
 raw=buffer.getvalue();assert len(raw)<=8*1024*1024
 print(json.dumps(dict(archive_base64=base64.b64encode(raw).decode(),archive_sha256=hashlib.sha256(raw).hexdigest())))
'''


def restore_verified(raw, destination, directory):
    """Extract bounded regular trees and verify types/lengths/hashes; reject duplicate, symlink or escaping members.

    destination must be new; retain partial extraction on failure. Return file/tree-entry counts for records.
    Never restore ownership/devices/FIFOs or execute archive contents.
    """
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    prefix = Path(directory)
    if prefix.parent != Path('/run') or not prefix.name.startswith('h102-gui-session-'):
        raise ValueError('IIO_ARCHIVE_DIRECTORY')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = archive.getmembers()
        names = [m.name.rstrip('/') for m in members]
        if len(members) > 4097 or len(names) != len(set(names)):
            raise ValueError('IIO_ARCHIVE_MEMBER_COUNT')
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or '..' in path.parts or
                    not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError('IIO_ARCHIVE_MEMBER')
        if sum(m.size for m in members) > 9*1024*1024:
            raise ValueError('IIO_ARCHIVE_EXPANSION')
        manifest = json.load(archive.extractfile('manifest.json'))
        expected = {'manifest.json'} | {p.lstrip('/') for p in manifest}
        if set(names) != expected:
            raise ValueError('IIO_ARCHIVE_MANIFEST_SET')
        files = 0
        for path, entry in manifest.items():
            original = Path(path)
            if not original.is_relative_to(prefix) or original == Path('/'):
                raise ValueError('IIO_ARCHIVE_MANIFEST_PATH')
            member = archive.getmember(path.lstrip('/'))
            target = destination/path.lstrip('/')
            if entry['kind'] == 'directory' and member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif entry['kind'] == 'file' and member.isfile():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                data = archive.extractfile(member).read()
                if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                    raise ValueError('IIO_ARCHIVE_RESTORE_SHA')
                with target.open('xb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                # Reread persisted files rather than validating downloaded bytes only in memory.
                if hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
                    raise ValueError('IIO_ARCHIVE_RESTORE_READBACK')
                files += 1
            else:
                raise ValueError('IIO_ARCHIVE_MANIFEST_KIND')
        (destination/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    # Persist every new directory/manifest before requesting deletion of board originals.
    for path in [destination/'manifest.json', *sorted((p for p in destination.rglob('*') if p.is_dir()),
                                                     key=lambda p: len(p.parts), reverse=True), destination]:
        fd = os.open(path, os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return dict(files=files, entries=len(manifest), restore_verified=True)


def retire(controller, link, connection):
    """Archive this session through the fixed API after normal post; never scan/delete other sessions."""
    from h102_control.client import Client
    client = Client(connection)
    identifier = link.api_receipt['session_id']
    description = client.request('session.evidence.open', dict(session_id=identifier), timeout=75)
    size, chunk = description['bytes'], description['chunk_size']
    if type(size) is not int or not 0 < size <= 8*1024*1024 or chunk != 65536:
        raise RuntimeError('IIO_ARCHIVE_GEOMETRY')
    pieces = []
    for index in range((size+chunk-1)//chunk):
        item = client.request('session.evidence.chunk', dict(session_id=identifier, index=index))
        data = base64.b64decode(item['data'], validate=True)
        if item['index'] != index or len(data) != min(chunk, size-index*chunk):
            raise RuntimeError('IIO_ARCHIVE_CHUNK_IDENTITY')
        pieces.append(data)
    raw = b''.join(pieces)
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) != size or sha != description['archive_sha256']:
        raise RuntimeError('IIO_ARCHIVE_TRANSFER_SHA')
    output = controller.run_output/'runtime-archive'
    output.mkdir(mode=0o700)
    with (output/'runtime.tar.gz').open('xb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    verified = restore_verified(raw, output/'restored', link.directory)
    controller._save('runtime-archive-verified.json', dict(verified, archive_sha256=sha))
    for path in (output, output.parent):
        fd = os.open(path, os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    receipt = client.request('session.evidence.retire', dict(session_id=identifier, archive_sha256=sha), timeout=75)
    if receipt.get('status') != 'RETIRED_EXACT_VERIFIED' or receipt.get('archive_sha256') != sha:
        raise RuntimeError('IIO_ARCHIVE_RETIRE_RECEIPT')
    controller._save('runtime-retire.json', receipt)

# End of file: iio_runtime_archive.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
