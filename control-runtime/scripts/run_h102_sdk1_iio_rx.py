#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       run_h102_sdk1_iio_rx.py
@brief      Capture SDK1 dual-RX snapshots through real network libiio and finite board permission.
@details    Select approved profiles in sequence; the board manager retains shared locks across main/post.
            Stop on failure, restore 40/20 and preserve raw IQ/XML/service results. Never start TX or USB.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    1.0.0
@date       2026-10-08
@note       Read SSH/sudo credentials interactively into RAM only; finite noise does not qualify continuity or RF performance.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
import ctypes.util
import getpass
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time
import uuid

import numpy as np
import paramiko
from h102_iio_finite import FiniteIIO

PROFILES = ['40msps-20mhz', '20msps-10mhz', '8msps-4mhz',
            '0p192msps-0p05mhz', '61p44msps-40mhz']
LEASE_SCRIPT = '/opt/h102-sdk1-iio-lock-v2-20261008/scripts/h102_iio_rx_lease.py'


def save(path: Path, value) -> None:
    """Write receipts in a new evidence directory without overwriting sealed results."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class Board:
    """Use a separate strictly host-key-pinned SSH connection per call; credentials remain in RAM."""
    def __init__(self, host, user, password, known):
        self.host, self.user, self.password, self.known = host, user, password, known

    def run(self, command: str, timeout=80) -> dict:
        """Bound root commands; passwords use SSH stdin only, never command arguments or results."""
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.known))
        client.connect(self.host, username=self.user, password=self.password,
                       allow_agent=False, look_for_keys=False, timeout=8)
        try:
            wire = "sudo -S -p '' /bin/sh -c " + shlex.quote(command)
            stdin, stdout, stderr = client.exec_command(wire, timeout=timeout)
            stdin.write(self.password + '\n'); stdin.flush(); stdin.channel.shutdown_write()
            channel = stdout.channel
            output, errors = [], []
            deadline = time.monotonic() + timeout
            while True:
                if channel.recv_ready():
                    output.append(channel.recv(65536))
                if channel.recv_stderr_ready():
                    errors.append(channel.recv_stderr(65536))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('ROOT_COMMAND_DEADLINE')
                time.sleep(.01)
            return dict(exit=channel.recv_exit_status(), stdout=b''.join(output).decode(errors='replace'),
                        stderr=b''.join(errors).decode(errors='replace'))
        finally:
            client.close()


class Control:
    """Access software IIO configuration attributes only; create no buffers or data enables."""
    def __init__(self, host):
        self.lib = C.CDLL(ctypes.util.find_library('iio'))
        for name, args, result in [
                ('iio_create_context_from_uri', [C.c_char_p], C.c_void_p),
                ('iio_context_destroy', [C.c_void_p], None),
                ('iio_context_set_timeout', [C.c_void_p, C.c_uint], C.c_int),
                ('iio_context_find_device', [C.c_void_p, C.c_char_p], C.c_void_p),
                ('iio_device_attr_read', [C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t], C.c_ssize_t),
                ('iio_device_attr_write', [C.c_void_p, C.c_char_p, C.c_char_p], C.c_ssize_t)]:
            method = getattr(self.lib, name); method.argtypes = args; method.restype = result
        self.ctx = self.lib.iio_create_context_from_uri(('ip:' + host).encode())
        if not self.ctx:
            raise RuntimeError('IIO_CONTROL_CONTEXT')
        self.lib.iio_context_set_timeout(self.ctx, 5000)
        self.dev = self.lib.iio_context_find_device(self.ctx, b'h102-profile-control')
        if not self.dev:
            self.close(); raise RuntimeError('IIO_CONTROL_DEVICE')

    def close(self):
        """Release the actual network control connection."""
        if self.ctx:
            self.lib.iio_context_destroy(self.ctx); self.ctx = None

    def read(self, name):
        """Strip trailing NUL using libiio's returned length; preserve errno on failure."""
        buffer = C.create_string_buffer(4096)
        size = self.lib.iio_device_attr_read(self.dev, name.encode(), buffer, len(buffer))
        if size < 0:
            raise OSError(-size, 'IIO_ATTRIBUTE: ' + name)
        return buffer.raw[:size].decode().rstrip('\0\r\n')

    def select(self, profile: str) -> dict:
        """Wait for independent service validation before selecting; completion must match target and active parameters."""
        deadline = time.monotonic() + 40
        while self.read('status') in ('unavailable', 'queued', 'applying'):
            if time.monotonic() >= deadline:
                raise TimeoutError('CONTROL_NOT_READY')
            time.sleep(.25)
        if self.read('active') != profile:
            size = self.lib.iio_device_attr_write(self.dev, b'select', profile.encode())
            if size < 0:
                raise OSError(-size, 'IIO_SELECT')
            deadline = time.monotonic() + 210
            while self.read('status') in ('queued', 'applying'):
                if time.monotonic() >= deadline:
                    raise TimeoutError('IIO_APPLY_DEADLINE')
                time.sleep(.25)
            if self.read('status') != 'applied' or self.read('active') != profile:
                raise RuntimeError('IIO_APPLY_FAILED: ' + self.read('error'))
        return json.loads(self.read('active_info'))


def statistics(raw: bytes) -> dict:
    """Check finite IQ validity, rejecting all-zero/all-DC; noise statistics are not RF specifications."""
    values = np.frombuffer(raw, dtype='<i2').reshape(-1, 2).astype(np.float64)
    dc, std = values.mean(axis=0), values.std(axis=0)
    result = dict(samples=len(values), min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist(),
                  dc=dc.tolist(), ac_rms=std.tolist(),
                  clipping_fraction=float(np.mean((values == -32768) | (values == 32767))),
                  nonconstant=bool(np.all(std > 0)), continuity_proven=False)
    return result



def pn15_errors(raw: bytes) -> list[int]:
    """Check adjacent component words using actual RTL ad_pngen x15+x14+1 with 16-bit steps.

    Use received low 15 bits as state, without polynomial/order/inversion fitting; numerical checks separately reject zeros.
    A short per-sample window does not prove cross-buffer or long-term continuity.
    """
    words = np.frombuffer(raw, dtype='<u2').reshape(-1, 2).astype(np.uint32)
    if len(words) < 2:
        raise ValueError('PN15_MINIMUM_TWO_WORDS')
    state = words[:-1] & 32767
    predicted = np.zeros_like(state)
    for _ in range(16):
        bit = ((state >> 14) ^ (state >> 13)) & 1
        state = ((state << 1) | bit) & 32767
        predicted = (predicted << 1) | bit
    return np.count_nonzero(predicted != words[1:], axis=0).tolist()


def capture_pair(board: Board, host: str, directory: Path, info: dict, pattern: str = 'normal') -> None:
    """Retain board permission for both RX snapshots; close networking and await independent post even on failure."""
    session = '/run/h102-iio-rx-' + uuid.uuid4().hex
    command = '/usr/bin/python3 -B ' + LEASE_SCRIPT + ' manager --directory ' + session + ' --pattern ' + pattern
    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = executor.submit(board.run, command)
        started = False
        try:
            deadline = time.monotonic() + 10
            while True:
                result = board.run('test -f ' + session + '/ready.json && cat ' + session + '/ready.json')
                if result['exit'] == 0:
                    save(directory / 'ready.json', json.loads(result['stdout']))
                    started = True
                    break
                if manager.done():
                    save(directory / 'manager.json', manager.result())
                    raise RuntimeError('RX_MANAGER_NOT_READY')
                if time.monotonic() >= deadline:
                    raise TimeoutError('RX_READY_DEADLINE')
                time.sleep(.1)
            count = 0
            def permitted():
                """Check actual root permission for every buffer creation/retrieval; never use an always-true substitute."""
                nonlocal count
                count += 1
                result = board.run('/usr/bin/python3 -B ' + LEASE_SCRIPT + ' permit --directory ' + session)
                save(directory / f'permit-{count}.json', result)
                if result['exit']:
                    return False
                value = json.loads(result['stdout'])
                return value['permitted'] and value['configuration_id'] == info['configuration_id']
            with FiniteIIO('ip:' + host, permitted) as client:
                (directory / 'context.xml').write_bytes(client.xml)
                for channel in (1, 2):
                    raw, layout = client.capture(channel)
                    (directory / f'rx{channel}.bin').write_bytes(raw)
                    value = dict(layout=layout, configuration=info, statistics=statistics(raw),
                                 sha256=hashlib.sha256(raw).hexdigest(), pattern=pattern)
                    if pattern == 'ramp16':
                        words = np.frombuffer(raw, dtype='<u2').reshape(-1, 2).astype(np.int32)
                        mismatch = ((np.diff(words, axis=0) & 65535) != 1)
                        value['ramp'] = dict(delta_errors=mismatch.sum(axis=0).tolist(),
                            iq_errors=int(np.count_nonzero(words[:,0] != words[:,1])))
                    save(directory / f'rx{channel}.json', value)
                    if pattern == 'prbs15':
                        value['pn15_word_errors'] = pn15_errors(raw)
                        save(directory / f'rx{channel}.json', value)
                        if any(value['pn15_word_errors']):
                            raise RuntimeError('PN15_DATA_MISMATCH')
                    if pattern == 'ramp16' and (any(value['ramp']['delta_errors']) or value['ramp']['iq_errors']):
                        raise RuntimeError('RAMP16_DATA_MISMATCH')
                    if not value['statistics']['nonconstant']:
                        raise RuntimeError('RX_ZERO_OR_DC_ONLY')
        finally:
            prior_failure = sys.exc_info()[0] is not None
            if started:
                stop = board.run('/usr/bin/python3 -B ' + LEASE_SCRIPT + ' stop --directory ' + session)
                save(directory / 'stop.json', stop)
            result = manager.result(timeout=75)
            save(directory / 'manager.json', result)
            evidence = board.run('cat ' + session + '/request.json; cat ' + session + '/post.json') if started else dict(not_started=True)
            save(directory / 'board-session.json', dict(directory=session, started=started, result=evidence))
            if result['exit'] != 0 and not prior_failure:
                raise RuntimeError('RX_MANAGER_POST_FAILED')


def main() -> None:
    """Run a five-profile matrix in a new directory; retain/stop on failure and finally restore 40/20."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', choices=['normal','ramp16','prbs15'], default='normal')
    parser.add_argument('--profiles', nargs='+', default=PROFILES)
    parser.add_argument('--host', default='10.168.1.131')
    parser.add_argument('--username', default='logicrf')
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass('H102 SSH/sudo password: ')
    args.output.mkdir(parents=True, exist_ok=False)
    board = Board(args.host, args.username, password, args.known_hosts)
    control = Control(args.host)
    summary = dict(profiles=[], usb_used=False, tx_started=False, data_continuity_proven=False)
    try:
        for index, profile in enumerate(args.profiles, 1):
            print(json.dumps(dict(event='begin', index=index, profile=profile)), flush=True)
            directory = args.output / profile; directory.mkdir()
            info = control.select(profile)
            save(directory / 'active-info.json', info)
            capture_pair(board, args.host, directory, info, args.pattern)
            summary['profiles'].append(profile)
            save(args.output / 'summary.json', summary)
            print(json.dumps(dict(event='passed', profile=profile)), flush=True)
    except Exception as exc:
        summary['error'] = repr(exc)
        raise
    finally:
        try:
            summary['final'] = control.select('40msps-20mhz')
        finally:
            save(args.output / 'summary.json', summary)
            control.close()


if __name__ == '__main__':
    main()
# End of file: run_h102_sdk1_iio_rx.py
