#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
@file install_usb_host_portable.py
@brief Prepare, stage and switch an H102 USB host installation for a selected ordinary user.
@details Preparation does not access USB. Installation verifies the complete approved payload
         before writing a new root directory. Activation and rollback preserve the original
         device lock and fixed local service. Vendor libraries are supplied separately.
         Receive/transmit code and independent recovery protocols remain unchanged.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-24
"""
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shlex
import stat
import subprocess
import time

VERSION = '1.0.0-r2'
LIBRARY_SHA = '949445eb019a6fe3e314e54cc45af1b8ddb1fcdf0c377f1144a59143d0a08752'
UNIT = Path('/etc/systemd/system/logicrf-h102-usb-api.service')
LEGACY_LOCK = Path('/data/logicrf/h102-usb-acceptance/locks/ft601-000000000001.lock')
DEFAULT_LOCK_DIRECTORY = Path('/var/lib/logicrf/hawk-h102/usb-locks')
SOURCE = Path(__file__).resolve().parents[1]
MAX_FILE = 8 * 1024 * 1024
PAYLOAD = {
    'private/usb_host_guard.py': 'scripts/usb_host_guard.py',
    'private/usb_host_archive.py': 'scripts/usb_host_archive.py',
    'private/h102_rf_manager_refs.py': 'transport/support/h102_rf_manager_refs.py',
    'broker/usb_host_broker.py': 'scripts/usb_host_broker.py',
    'broker/h102_control/__init__.py': 'python/h102_control/__init__.py',
    'broker/h102_control/protocol.py': 'python/h102_control/protocol.py',
}
BINARIES = tuple('logicrf-ft601-' + direction for direction in ('rx', 'tx', 'duplex'))
OWN_SOURCE = tuple(sorted(set(PAYLOAD.values()) | {
    'scripts/install_usb_host_portable.py', 'tools/usb-host/CMakeLists.txt',
    'docs/USB-HOST-PORTABLE.md', 'transport/ft601_rx.cc', 'transport/ft601_tx.cc',
    'transport/ft601_duplex.cc', 'transport/h102_ft601_persistent_dma.c',
    'transport/h102_ft601_persistent_dma.h', 'transport/h102_ft601_completion.h',
}))


def digest(raw):
    """Return the SHA-256 of complete bytes already held in memory."""
    return hashlib.sha256(raw).hexdigest()


def regular(path):
    """Read a bounded regular file through O_NOFOLLOW; reject identity changes during the read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise ValueError('REGULAR_BOUNDED_FILE_REQUIRED')
        raw = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after) or len(raw) != before.st_size:
        raise ValueError('INPUT_CHANGED_DURING_READ')
    return raw


def plain(path):
    """Preserve an absolute path; reject symlink ancestors, traversal, controls and systemd expansion."""
    path = Path(path).absolute()
    if '..' in path.parts or any(c in str(path) for c in '\n\r\0%'):
        raise ValueError('PLAIN_ABSOLUTE_PATH_REQUIRED')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('SYMLINK_PATH_REJECTED')
    return path


def root_path(path, *, directory=False, private=False):
    """Require a root-owned file or directory and root-owned ancestors without external write access."""
    path = plain(path)
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid or info.st_mode & (0o077 if private else 0o022):
        raise PermissionError('UNTRUSTED_ROOT_PATH: ' + str(path))
    for parent in path.parents:
        value = parent.lstat()
        if value.st_uid or value.st_mode & 0o022 or not stat.S_ISDIR(value.st_mode):
            raise PermissionError('UNTRUSTED_ROOT_ANCESTOR')
    return path


def root_parents(path):
    """Create missing directories only below a trusted root parent; never take over user directories."""
    path = plain(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    root_path(path, directory=True)
    for child in reversed(missing):
        child.mkdir(mode=0o755)
        root_path(child, directory=True)


def save(path, value, *, exclusive=True):
    """Save private JSON exclusively, or atomically replace a known state record when requested."""
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    target = path if exclusive else path.with_name(path.name + '.new')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        os.replace(target, path)


def expected_members():
    """Return the fixed file set; manifests cannot introduce additional code, paths or vendor files."""
    return set(PAYLOAD) | {'public/' + name for name in BINARIES} | {
        'source/' + name for name in OWN_SOURCE}


def prepare(build_root, output):
    """Freeze existing ELF files and LogicRF sources without USB access, vendor copies or service startup."""
    build_root, output = plain(build_root), plain(output)
    files = {name: regular(SOURCE / src) for name, src in PAYLOAD.items()}
    for name in BINARIES:
        raw = regular(build_root / name)
        if raw[:6] != b'\x7fELF\x02\x01' or raw[18:20] != b'\x3e\x00':
            raise ValueError('X86_64_ELF_REQUIRED: ' + name)
        files['public/' + name] = raw
    files.update({'source/' + name: regular(SOURCE / name) for name in OWN_SOURCE})
    if set(files) != expected_members():
        raise ValueError('INTERNAL_PAYLOAD_SET')
    if sum(len(raw) for raw in files.values()) > 32 * 1024 * 1024:
        raise ValueError('BUNDLE_TOTAL_LIMIT')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, raw in files.items():
        target = output / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest = {'schema': 1, 'version': VERSION, 'external_library_sha256': LIBRARY_SHA,
                'files': {name: {'bytes': len(raw), 'sha256': digest(raw)} for name, raw in sorted(files.items())}}
    save(output / 'manifest.json', manifest)
    return {'status': 'PREPARED_NO_DEVICE_ACCESS', 'bundle': str(output),
            'manifest_sha256': digest(regular(output / 'manifest.json')), 'files': len(files)}


def read_bundle(bundle, approved_sha):
    """Verify an administrator-approved manifest hash and retain each checked payload in memory."""
    bundle = plain(bundle)
    raw = regular(bundle / 'manifest.json')
    if not re.fullmatch('[0-9a-f]{64}', approved_sha) or digest(raw) != approved_sha:
        raise ValueError('APPROVED_MANIFEST_SHA_REQUIRED')
    manifest = json.loads(raw)
    if (set(manifest) != {'schema', 'version', 'external_library_sha256', 'files'}
            or manifest['schema'] != 1 or manifest['version'] != VERSION
            or manifest['external_library_sha256'] != LIBRARY_SHA
            or set(manifest['files']) != expected_members()):
        raise ValueError('BUNDLE_CONTRACT_MISMATCH')
    actual = set()
    for path in bundle.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('UNSAFE_BUNDLE_MEMBER')
        if path.is_file():
            actual.add(str(path.relative_to(bundle)))
    if actual != expected_members() | {'manifest.json'}:
        raise ValueError('EXTRA_OR_MISSING_BUNDLE_FILE')
    files = {}
    total = 0
    for name, expected in manifest['files'].items():
        value = regular(bundle / name)
        total += len(value)
        if total > 32 * 1024 * 1024:
            raise ValueError('BUNDLE_TOTAL_LIMIT')
        if {'bytes': len(value), 'sha256': digest(value)} != expected:
            raise ValueError('BUNDLE_FILE_DRIFT: ' + name)
        files[name] = value
    return manifest, files


def existing():
    """Read the fixed service and pinned deployment without importing or executing previous code."""
    if not UNIT.exists():
        return None
    raw = regular(root_path(UNIT))
    commands = [line[10:] for line in raw.decode().splitlines() if line.startswith('ExecStart=')]
    if len(commands) != 1:
        raise ValueError('EXISTING_UNIT_NOT_RECOGNIZED')
    argv = shlex.split(commands[0])
    try:
        guard = root_path(argv[argv.index('--guard') + 1], private=True)
        pin = argv[argv.index('--guard-sha256') + 1]
        uid = int(argv[argv.index('--uid') + 1])
    except (ValueError, IndexError) as error:
        raise ValueError('EXISTING_UNIT_NOT_RECOGNIZED') from error
    if digest(regular(guard)) != pin:
        raise ValueError('EXISTING_GUARD_DRIFT')
    deployment = json.loads(regular(root_path(guard.parent / 'deployment.json', private=True)))
    if deployment['uid'] != uid:
        raise ValueError('EXISTING_UID_DRIFT')
    return {'unit': raw, 'deployment': deployment}


def account(name):
    """Select an existing ordinary login user; do not create accounts or authorize root or service users."""
    user = pwd.getpwnam(name)
    if user.pw_uid < 1000 or user.pw_shell.endswith(('nologin', '/false')):
        raise ValueError('ORDINARY_LOGIN_USER_REQUIRED')
    return user


def choose_lock(explicit, serial, previous):
    """Reuse the original registered lock; create a root-controlled serial lock only on a new host."""
    if previous:
        old = previous['deployment']
        lock = plain(old['lock'])
        if explicit and plain(explicit) != lock:
            raise ValueError('EXISTING_LOCK_MUST_BE_REUSED')
        info = lock.lstat()
        if [info.st_dev, info.st_ino, info.st_uid] != old['lock_identity']:
            raise ValueError('EXISTING_LOCK_DRIFT')
        if old['serial'] != serial:
            raise ValueError('EXISTING_SERIAL_MISMATCH')
        return lock, False
    if LEGACY_LOCK.exists():
        if not explicit or plain(explicit) != plain(LEGACY_LOCK):
            raise ValueError('LEGACY_LOCK_MUST_BE_SELECTED')
        return plain(explicit), False
    if explicit:
        return root_path(explicit, private=True), False
    lock = DEFAULT_LOCK_DIRECTORY / ('ft601-' + serial + '.lock')
    root_parents(lock.parent)
    created = not lock.exists()
    if created:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    root_path(lock, private=True)
    return lock, created


def lock_info(path, uid):
    """Accept a private original lock owned by root or the selected user; preserve its owner and inode."""
    info = plain(path).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, uid)
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise PermissionError('PRIVATE_ORIGINAL_LOCK_REQUIRED')
    return info


def install(bundle, approved_sha, user_name, project_root, install_root, library, serial, lock_file=None):
    """Verify all inputs before creating a new root installation; activation is a separate command."""
    if os.geteuid():
        raise PermissionError('ADMIN_INSTALL_REQUIRED')
    manifest, files = read_bundle(bundle, approved_sha)
    user = account(user_name)
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', serial):
        raise ValueError('SERIAL_FORMAT')
    project_root, install_root = plain(project_root), plain(install_root)
    socket_root = project_root / 'envs/gr-usb'
    info = socket_root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
        raise PermissionError('USER_PRIVATE_SOCKET_DIRECTORY_REQUIRED')
    if len(os.fsencode(str(socket_root / ('0' * 32 + '.sock')))) >= 108:
        raise ValueError('UNIX_SOCKET_PATH_TOO_LONG')
    sdk = regular(plain(library))
    if digest(sdk) != LIBRARY_SHA:
        raise ValueError('EXTERNAL_D3XX_SHA_MISMATCH')
    if install_root.exists():
        raise FileExistsError('INSTALL_ROOT_EXISTS')
    previous = existing()
    if previous and previous['deployment']['uid'] != user.pw_uid:
        raise ValueError('EXISTING_UID_MUST_BE_PRESERVED')
    lock, created = choose_lock(lock_file, serial, previous)
    lock_identity = lock_info(lock, user.pw_uid)
    root_parents(install_root.parent)
    install_root.mkdir(mode=0o755)
    for directory, mode in [('private', 0o700), ('broker', 0o700), ('broker/h102_control', 0o700), ('public', 0o755)]:
        (install_root / directory).mkdir(mode=mode)
    installed = {}
    payloads = {name: raw for name, raw in files.items() if not name.startswith('source/')}
    payloads.update({'public/libftd3xx.so': sdk, 'public/libftd3xx.so.1.1.8': sdk})
    for name, raw in payloads.items():
        path = install_root / name
        path.write_bytes(raw)
        path.chmod(0o755 if name.startswith('public/logicrf-') else (0o644 if name.startswith('public/') else 0o600))
        if digest(regular(path)) != digest(raw):
            raise RuntimeError('INSTALLED_FILE_READBACK')
        installed[name] = digest(raw)
    deployment = {'schema': 1, 'project_root': str(project_root), 'serial': serial,
                  'uid': user.pw_uid, 'gid': user.pw_gid, 'lock': str(lock),
                  'lock_identity': [lock_identity.st_dev, lock_identity.st_ino, lock_identity.st_uid],
                  'socket_root': str(socket_root), 'public_root': str(install_root / 'public'),
                  'sources': {Path(name).name: sha for name, sha in installed.items() if name.startswith('private/')},
                  'public_files': {Path(name).name: sha for name, sha in installed.items() if name.startswith('public/')}}
    save(install_root / 'private/deployment.json', deployment)
    installed['private/deployment.json'] = digest(regular(install_root / 'private/deployment.json'))
    receipt = {'schema': 1, 'version': VERSION, 'status': 'STAGED_NOT_ACTIVATED',
               'root': str(install_root), 'user': user_name, 'uid': user.pw_uid,
               'manifest_sha256': approved_sha, 'installed_files': installed,
               'lock_created': created, 'source_files': {name: value for name, value in manifest['files'].items() if name.startswith('source/')}}
    save(install_root / 'installation.json', receipt)
    return receipt


def installed(root):
    """Recheck the complete root installation before switching; a historical receipt is insufficient."""
    root = root_path(root, directory=True)
    receipt = json.loads(regular(root_path(root / 'installation.json', private=True)))
    if receipt['root'] != str(root) or receipt['version'] != VERSION:
        raise ValueError('INSTALLATION_IDENTITY')
    for name, sha in receipt['installed_files'].items():
        if '..' in PurePosixPath(name).parts or PurePosixPath(name).is_absolute():
            raise ValueError('INSTALLATION_MEMBER_PATH')
        path = root_path(root / name, private=not name.startswith('public/'))
        if digest(regular(path)) != sha:
            raise ValueError('INSTALLED_FILE_DRIFT: ' + name)
    return receipt, json.loads(regular(root / 'private/deployment.json'))


def command(*args):
    """Run one fixed system command with a deadline and retain diagnostics for the caller."""
    return subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def service_state():
    """Read active and enabled states without treating missing or failed services as running."""
    def get(action):
        result = subprocess.run(['systemctl', action, UNIT.name], capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    return {'active': get('is-active'), 'enabled': get('is-enabled')}


def unit_text(root, deployment):
    """Generate the fixed service with quoted paths, including paths containing ordinary spaces."""
    guard = root / 'private/usb_host_guard.py'
    argv = ['/usr/bin/python3', '-B', str(root / 'broker/usb_host_broker.py'), '--guard', str(guard),
            '--guard-sha256', digest(regular(guard)), '--uid', str(deployment['uid']), '--inline-observation']
    line = ' '.join(json.dumps(value, ensure_ascii=False) for value in argv)
    return ('[Unit]\nDescription=LogicRF H102 local USB control API\n[Service]\nType=simple\n'
            'User=root\nUMask=0077\nRuntimeDirectory=logicrf-h102-usb\nRuntimeDirectoryMode=0755\n'
            'Environment=PYTHONNOUSERSITE=1\nEnvironment=PATH=/usr/sbin:/usr/bin:/sbin:/bin\n'
            'ExecStart=' + line + '\nRestart=no\n[Install]\nWantedBy=multi-user.target\n').encode()


@contextmanager
def idle(deployment, guard, require_usb):
    """Hold the original lock while checking USB idleness; offline activation requires no FT601."""
    lock = plain(deployment['lock'])
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r+b') as stream:
        info = os.fstat(stream.fileno())
        if [info.st_dev, info.st_ino, info.st_uid] != deployment['lock_identity']:
            raise ValueError('ORIGINAL_LOCK_DRIFT')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spec = importlib.util.spec_from_file_location('h102_installed_guard', guard)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.deployment()
        if require_usb:
            identity = module.identity(deployment)
            module.unused(identity['node'])
        else:
            for device in Path('/sys/bus/usb/devices').iterdir():
                if ((device / 'idVendor').exists()
                        and (device / 'idVendor').read_text().strip() == '0403'
                        and (device / 'idProduct').read_text().strip() == '601f'):
                    raise ValueError('OFFLINE_REQUIRES_NO_FT601')
        yield


def wait_ready(uid):
    """Wait for the selected user to own the listening 0600 socket; active alone is not readiness."""
    path = Path('/run/logicrf-h102-usb/control.sock')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o600:
                command('systemctl', 'is-active', '--quiet', UNIT.name)
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError('BROKER_SOCKET_NOT_READY')


def replace_unit(raw):
    """Atomically replace only the fixed service file; callers cannot choose a unit or shell command."""
    temporary = UNIT.with_name(UNIT.name + '.portable-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, UNIT)


def restore(previous, state):
    """Restore previous service bytes and active/enabled state; propagate any recovery failure."""
    command('systemctl', 'stop', UNIT.name)
    if previous is None:
        command('systemctl', 'disable', UNIT.name)
        UNIT.unlink()
    else:
        replace_unit(previous)
    command('systemctl', 'daemon-reload')
    if previous is not None:
        command('systemctl', 'enable' if state['enabled'] == 'enabled' else 'disable', UNIT.name)
        if state['active'] == 'active':
            command('systemctl', 'start', UNIT.name)


def activate(root, previous_sha, require_usb=True):
    """Check the previous unit and original lock, then switch with recorded failure recovery."""
    if os.geteuid():
        raise PermissionError('ADMIN_ACTIVATION_REQUIRED')
    root = plain(root)
    _, deployment = installed(root)
    previous = existing()
    old = previous['unit'] if previous else None
    if (old is not None and digest(old) != previous_sha) or (old is None and previous_sha):
        raise ValueError('PREVIOUS_UNIT_SHA_REQUIRED')
    if previous and (previous['deployment']['uid'] != deployment['uid'] or
                     previous['deployment']['lock_identity'] != deployment['lock_identity']):
        raise ValueError('ORIGINAL_USER_AND_LOCK_REQUIRED')
    activation = root / 'activation.json'
    if activation.exists():
        raise FileExistsError('ACTIVATION_ALREADY_RECORDED')
    state = service_state()
    if old is not None and state['active'] not in ('active', 'inactive'):
        raise RuntimeError('EXISTING_SERVICE_STATE_UNSUITABLE')
    if old is not None and state['enabled'] not in ('enabled', 'disabled'):
        raise RuntimeError('EXISTING_ENABLE_STATE_UNSUITABLE')
    raw = unit_text(root, deployment)
    candidate = root / UNIT.name
    candidate.write_bytes(raw)
    candidate.chmod(0o600)
    command('systemd-analyze', 'verify', str(candidate))
    record = {'status': 'ACTIVATING', 'new_unit_sha256': digest(raw),
              'previous_unit_sha256': digest(old) if old else None, 'previous_state': state,
              'require_usb': require_usb}
    if old is not None:
        (root / 'previous-unit.service').write_bytes(old)
        (root / 'previous-unit.service').chmod(0o600)
    with idle(deployment, root / 'private/usb_host_guard.py', require_usb):
        current = regular(UNIT) if UNIT.exists() else None
        if current != old:
            raise RuntimeError('CONCURRENT_UNIT_CHANGE')
        save(activation, record)
        try:
            if old is not None:
                command('systemctl', 'stop', UNIT.name)
            replace_unit(raw)
            command('systemctl', 'daemon-reload')
            command('systemctl', 'enable', '--now', UNIT.name)
            command('systemctl', 'is-active', '--quiet', UNIT.name)
            wait_ready(deployment['uid'])
        except BaseException:
            try:
                restore(old, state)
                record['status'] = 'FAILED_ORIGINAL_SERVICE_RESTORED'
            except BaseException:
                record['status'] = 'FAILED_RESTORE_REQUIRED'
                save(activation, record, exclusive=False)
                raise
            save(activation, record, exclusive=False)
            raise
    record['status'] = 'ACTIVE_NOT_DATA_QUALIFIED'
    save(activation, record, exclusive=False)
    return record


def rollback(root, require_usb=True):
    """Restore the previous service selection while retaining version files, locks and receipts."""
    if os.geteuid():
        raise PermissionError('ADMIN_ROLLBACK_REQUIRED')
    root = plain(root)
    _, deployment = installed(root)
    record = json.loads(regular(root_path(root / 'activation.json', private=True)))
    if record['status'] != 'ACTIVE_NOT_DATA_QUALIFIED' or digest(regular(UNIT)) != record['new_unit_sha256']:
        raise ValueError('CURRENT_UNIT_OR_ACTIVATION_DRIFT')
    previous = regular(root / 'previous-unit.service') if record['previous_unit_sha256'] else None
    if previous is not None and digest(previous) != record['previous_unit_sha256']:
        raise ValueError('PREVIOUS_UNIT_BACKUP_DRIFT')
    with idle(deployment, root / 'private/usb_host_guard.py', require_usb):
        try:
            restore(previous, record['previous_state'])
            state = service_state()
            before = record['previous_state']
            if previous is None:
                if UNIT.exists() or state['active'] in ('active', 'activating'):
                    raise RuntimeError('FRESH_HOST_SERVICE_STILL_SELECTED')
            elif (state['active'] != before['active'] or state['enabled'] != before['enabled']):
                raise RuntimeError('PREVIOUS_SERVICE_STATE_NOT_RESTORED')
        except BaseException:
            record['status'] = 'ROLLBACK_FAILED_RESTORE_REQUIRED'
            save(root / 'activation.json', record, exclusive=False)
            raise
    record['status'] = 'ROLLED_BACK_PAYLOAD_AND_LOCK_RETAINED'
    save(root / 'activation.json', record, exclusive=False)
    return record


def main():
    """Dispatch separate deployment actions; require idle USB unless explicit offline setup is requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--build-root', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    stage = sub.add_parser('install')
    stage.add_argument('--bundle', type=Path, required=True)
    stage.add_argument('--manifest-sha256', required=True)
    stage.add_argument('--user', required=True)
    stage.add_argument('--project-root', type=Path, required=True)
    stage.add_argument('--install-root', type=Path, required=True)
    stage.add_argument('--d3xx-library', type=Path, required=True)
    stage.add_argument('--serial', default='000000000001')
    stage.add_argument('--lock-file', type=Path)
    for action in ('activate', 'rollback'):
        operation = sub.add_parser(action)
        operation.add_argument('--install-root', type=Path, required=True)
        operation.add_argument('--offline', action='store_true')
        if action == 'activate':
            operation.add_argument('--previous-unit-sha256')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.build_root, args.output)
    elif args.action == 'install':
        result = install(args.bundle, args.manifest_sha256, args.user, args.project_root,
                         args.install_root, args.d3xx_library, args.serial, args.lock_file)
    elif args.action == 'activate':
        result = activate(args.install_root, args.previous_unit_sha256, not args.offline)
    else:
        result = rollback(args.install_root, not args.offline)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

# End of file: install_usb_host_portable.py
# Contact: info@logicrf.com | Website: https://www.logicrf.com
