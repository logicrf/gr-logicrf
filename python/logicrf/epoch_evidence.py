"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file epoch_evidence.py
@brief Persist USB generation originals/board write logs before issuing exact retirement acknowledgement.
@details Python handles evidence only, never IQ; exclusively create, fsync and read back every file's SHA.
         Do not acknowledge retirement on failure. Host disk retains history; active /run/RAM remain bounded.
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
import shutil
import tarfile


def durable(path, raw):
    """Create/fsync/read back evidence; fail on insufficient space or duplicate paths without overwriting history."""
    if shutil.disk_usage(path.parent).free < len(raw)+16*1024*1024:
        raise RuntimeError('USB_EVIDENCE_DISK_RESERVE')
    with path.open('xb') as target:
        target.write(raw);target.flush();os.fsync(target.fileno())
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(raw).digest():
        raise RuntimeError('USB_EVIDENCE_DURABLE_SHA')
    fd = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def json_file(path, value):
    """Save explicit current receipts only; never serialize connections or authentication."""
    durable(path, (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())


def retire_host(controller, epoch):
    """After actual host finish, archive/extract/check all hashes before acknowledging generation root retirement."""
    reply = controller._host('evidence_open',timeout=15)
    raw = base64.b64decode(reply['archive_base64'],validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = reply['manifest'];entries = manifest['entries']
    if (len(raw)>1024*1024 or digest!=reply['archive_sha256'] or len(entries)>128 or
            manifest['bytes']>4*1024*1024):
        raise RuntimeError('USB_HOST_ARCHIVE_WIRE')
    output = controller.run_output/'host-archives'/('%08d'%epoch)
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    durable(output/'runtime.tar.gz',raw);json_file(output/'manifest.json',manifest)
    restored=output/'restored';restored.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        members=archive.getmembers()
        names=[m.name for m in members]
        if len(names)!=len(set(names)) or set(names)!=set(entries):
            raise RuntimeError('USB_HOST_ARCHIVE_MEMBERS')
        for item in members:
            if (Path(item.name).name!=item.name or item.name in ('.','..') or
                    not(item.isfile() or item.isfifo()) or
                    entries[item.name]['fifo']!=item.isfifo() or item.size!=entries[item.name]['bytes']):
                raise RuntimeError('USB_HOST_ARCHIVE_MEMBER')
            if item.isfile():
                data=archive.extractfile(item).read()
                if hashlib.sha256(data).hexdigest()!=entries[item.name]['sha256']:
                    raise RuntimeError('USB_HOST_ARCHIVE_RESTORE_SHA')
                durable(restored/item.name,data)
    json_file(output/'verified.json',dict(archive_sha256=digest,files=len(entries),restore_verified=True))
    receipt=controller._host('evidence_retire',dict(archive_sha256=digest,manifest=manifest),timeout=15)
    if receipt.get('status')!='RETIRED_EXACT_VERIFIED' or receipt.get('archive_sha256')!=digest:
        raise RuntimeError('USB_HOST_ARCHIVE_RETIRE_UNKNOWN')
    json_file(output/'retired.json',receipt)
    return receipt


def checkpoint_audit(controller, epoch):
    """Transfer generation logs in chunks only while board STOPPED; clear originals only after confirmed persistence."""
    start=controller._command('audit_open')['audit']
    size=start['bytes'];identifier=start['id'];digest=start['sha256']
    if type(size) is not int or not 0<=size<=2*1024*1024:
        raise RuntimeError('USB_AUDIT_SIZE')
    pieces=[]
    first=start.pop('first_chunk',None)
    if first is not None:
        if (set(first)!={'id','index','data'} or first['id']!=identifier or
                type(first['index']) is not int or first['index']!=0 or not size):
            raise RuntimeError('USB_AUDIT_FIRST_CHUNK')
        data=base64.b64decode(first['data'],validate=True)
        if len(data)!=min(size,12288):raise RuntimeError('USB_AUDIT_FIRST_CHUNK_SIZE')
        pieces.append(data)
    for index in range(len(pieces),(size+12287)//12288):
        reply=controller._command('audit_chunk',audit_id=identifier,index=index)['audit']
        if reply['id']!=identifier or reply['index']!=index:
            raise RuntimeError('USB_AUDIT_CHUNK_IDENTITY')
        pieces.append(base64.b64decode(reply['data'],validate=True))
    raw=b''.join(pieces)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest:
        raise RuntimeError('USB_AUDIT_TRANSFER_SHA')
    output=controller.run_output/'board-write-audits'/('%08d'%epoch)
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    durable(output/'writes.jsonl',raw);json_file(output/'manifest.json',start)
    result=controller._command('audit_retire',audit_id=identifier,sha256=digest)['audit']
    if result.get('status')!='RETIRED_EXACT_VERIFIED' or result.get('sha256')!=digest:
        raise RuntimeError('USB_AUDIT_RETIRE_UNKNOWN')
    json_file(output/'retired.json',result)
    return result

# End of file: epoch_evidence.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
