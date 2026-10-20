"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_host_archive.py
@brief Archive/retire exact USB host sessions proven complete by independent main/post.
@details Fixed root guard only; recheck full manifest, identity, original lock and quotas before deletion.
         open preserves originals; client restore-SHA/fsync must precede hash acknowledgement. Never retry uncertain deletion.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import base64
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile


def snapshot(directory):
    """Accept root-private flat files/FIFOs only; inode/mtime/SHA manifests support independent second comparison."""
    root = directory.lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid() or root.st_mode & 0o077:
        raise RuntimeError('USB_ARCHIVE_DIRECTORY')
    entries = {};total = 0
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if (info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1 or
                not (stat.S_ISREG(info.st_mode) or stat.S_ISFIFO(info.st_mode))):
            raise RuntimeError('USB_ARCHIVE_MEMBER')
        entry = dict(mode=info.st_mode, inode=info.st_ino, mtime_ns=info.st_mtime_ns,
                     bytes=info.st_size, fifo=stat.S_ISFIFO(info.st_mode))
        if not entry['fifo']:
            total += info.st_size
            entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[path.name] = entry
        if len(entries) > 128 or total > 4*1024*1024:
            raise RuntimeError('USB_ARCHIVE_LIMIT')
    return dict(directory_inode=root.st_ino, entries=entries, bytes=total)


def terminal(directory, deployment, guard):
    """Require saved finish proof, current terminal state, idle device and original quotas together."""
    request = guard.request(directory, deployment, touch=False)
    final = json.loads((directory/'final.json').read_bytes())
    if (not final['main.json'].get('passed') or not final['post.json'].get('passed') or
            final['state.json'].get('running') is not False or
            final['native'].get('ExecMainStatus') != '0' or
            json.loads((directory/'main.json').read_bytes()) != final['main.json'] or
            json.loads((directory/'post.json').read_bytes()) != final['post.json']):
        raise RuntimeError('USB_ARCHIVE_FINAL_PROOF')
    state = subprocess.run(['systemctl','show',directory.name+'.service','-p','ActiveState','--value'],
                           capture_output=True,text=True,timeout=5,check=True).stdout.strip()
    if state not in ('inactive','failed'):
        raise RuntimeError('USB_ARCHIVE_UNIT_ACTIVE_OR_UNKNOWN')
    guard.unused(request['host']['node'])
    quota = json.loads((directory/'quota.json').read_bytes())
    if int(guard.QUOTA.read_text()) != quota['original']:
        raise RuntimeError('USB_ARCHIVE_QUOTA')


def evidence(directory, deployment, guard, value=None):
    """Protect exact sessions with original locks; return bounded tar or delete after verified client hash.

    value=None is read-only packaging; otherwise accept archive_sha256 and complete manifest only.
    Retain no root download cache, preventing per-generation cache/receipt growth; preserve originals on failure.
    """
    with guard.locked(deployment):
        terminal(directory, deployment, guard)
        manifest = snapshot(directory)
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as packed:
                for name in manifest['entries']:
                    packed.add(directory/name, arcname=name, recursive=False)
        raw = buffer.getvalue()
        if len(raw) > 1024*1024:
            raise RuntimeError('USB_ARCHIVE_WIRE_LIMIT')
        # Fixed gzip timestamps permit deterministic tar hashes without per-generation root caches.
        if value is None:
            return dict(archive_base64=base64.b64encode(raw).decode(),
                        archive_sha256=hashlib.sha256(raw).hexdigest(),manifest=manifest)
        if (set(value) != {'archive_sha256','manifest'} or
                not isinstance(value['archive_sha256'],str) or len(value['archive_sha256']) != 64 or
                value['manifest'] != manifest or
                value['archive_sha256'] != hashlib.sha256(raw).hexdigest()):
            raise RuntimeError('USB_ARCHIVE_ORIGINAL_CHANGED')
        # Guard retains original FT lock; root-private terminal directories have no valid writers; recheck before precise deletion.
        if snapshot(directory) != manifest:
            raise RuntimeError('USB_ARCHIVE_ORIGINAL_CHANGED')
        shutil.rmtree(directory)
        return dict(status='RETIRED_EXACT_VERIFIED',session=directory.name,
                    archive_sha256=value['archive_sha256'],files=len(manifest['entries']),
                    bytes=manifest['bytes'])

# End of file: usb_host_archive.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
