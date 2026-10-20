"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file compat.py
@brief Adapt H102 sessions to the fixed device API while retaining native ownership and independent post.
@details Install the adapter explicitly; send installed helper hashes only, never remote commands or
         uploaded source. Verify each runtime file SHA and record adapter identity separately.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
from threading import Thread
import time

from .channel import SessionChannel
from .client import Client, Connection
from .source_identity import installed_sources


@dataclass
class ApiBoard:
    """Store device address only; no run/exec; password=None supports legacy cleanup code."""
    connection: Connection
    password: None = None


def warm(link):
    """Open the fixed native entry; the existing SessionLink thread manages actual client pulses."""
    link.started = time.monotonic()
    sources = {name: hashlib.sha256(base64.b64decode(raw)).hexdigest()
               for name, raw in link.sources.items()}
    sources = installed_sources(sources)
    peer, reader, receipt = Client(link.connection).open_session(link.entrypoint, sources)
    channel = SessionChannel(peer, reader)
    try:
        if receipt['entrypoint'] != link.entrypoint or receipt['sources'] != sources:
            raise RuntimeError('CONTROL_API_NATIVE_RECEIPT_MISMATCH')
        link.directory = receipt['directory']
        link.api_receipt = receipt
        link.client = link.channel = channel
        link.timings['api_connected_seconds'] = time.monotonic()-link.started
        link.thread = Thread(target=link._io, name='h102-api-native-control', daemon=True)
        link.thread.start()
        if not link.warm_event.wait(15) or link.done.is_set():
            raise RuntimeError('CONTROL_API_NATIVE_WARM_FAILED: '+str(link.error or link.stderr)[-700:])
        link.timings['warm_ready_seconds'] = time.monotonic()-link.started
        return link
    except Exception:
        channel.close()
        link.close(stop=False)
        raise


def install(backend, session_module):
    """Replace ordinary control explicitly; all derived Links use the same restricted API transport.

    Verify runtime files against application locks first. Installation neither contacts devices nor changes old files.
    No network SSH fallback; missing device services produce an upgrade requirement.
    """
    if getattr(backend, 'CONTROL_TRANSPORT', None) == 'h102-api-v1':
        return backend
    session_module.SessionLink.warm = warm
    backend.Connection = Connection
    backend.SessionLink = session_module.SessionLink
    backend.CONTROL_TRANSPORT = 'h102-api-v1'

    def board_for(connection):
        connection.validate()
        return ApiBoard(connection)

    def inspect(connection, profile=None, prepare_control=False, control_output=None, current_state=None):
        """Read an idle snapshot; reuse a freshly checked snapshot within one prepare to avoid duplicate status.

        current_state is for one synchronous selection, not cross-session caching. The server still checks current boot,
        the full profile transaction and selected state. The prepared actor independently locks/checks actual configuration.
        """
        started = time.monotonic()
        client = Client(connection)
        state = deepcopy(current_state) if current_state is not None else client.status()
        if profile is not None and state['info']['active'] != profile:
            state = client.request('profile.select', dict(profile=profile, boot_id=state['info']['boot_id']), timeout=240)
        if state['info']['api'] != '68.21.6':
            raise RuntimeError('CONTROL_API_REQUIRES_H102_SDK1_68_21_6')
        if prepare_control:
            if control_output is None:
                state['_warm_link'] = backend.warm_control(connection, state['info'])
            else:
                link = backend.prepare_idle_control(connection, state['info'], [1], control_output,
                                                    discover_radio=True)
                try:
                    snapshot = link.ready['connection_snapshot']
                    if (snapshot['parameters'] != state['info']['parameters'] or
                            snapshot['identity'] != state['actual']['identity']):
                        raise RuntimeError('CONTROL_API_PREPARED_IDENTITY_CHANGED')
                    state['actual'].update(snapshot)
                    state['info']['radio'] = snapshot['radio']
                    link.info = deepcopy(state['info'])
                    link.channels = tuple(link.ready['channels'])
                    state['_idle_link'] = link
                except Exception:
                    backend.close_idle_control(link)
                    raise
        state['connection_timings'] = dict(total_seconds=time.monotonic()-started, transport='h102-api-v1')
        return state

    def collect_session(board, directory, boot):
        """Read fixed receipts by service-issued session ID; await independent post after disconnect before success."""
        client = Client(board.connection)
        if client.request('metadata')['boot_id'] != boot:
            raise RuntimeError('CONTROL_API_BOOT_CHANGED_BEFORE_COLLECTION')
        deadline = time.monotonic()+95
        while True:
            native = client.request('session.records', dict(session_id=Path(directory).name.removeprefix('h102-gui-session-')))
            properties = native.get('_unit_properties', '')
            if ('post.json' in native or 'ActiveState=failed\n' in properties or
                    'ActiveState=inactive\n' in properties or time.monotonic() >= deadline):
                return native
            time.sleep(.2)

    def command(*args, **kwargs):
        raise RuntimeError('CONTROL_API_GENERIC_COMMAND_REMOVED_USE_TYPED_OPERATION')

    def tune_rx(connection, info, channels, frequency, output):
        state = dict(info=info, actual=dict(radio=info['radio']))
        return backend.tune_rx_idle(connection, state, channels, frequency, output)

    backend.board_for = board_for
    backend.inspect = inspect
    backend.inspect_prepared = lambda connection, profile, output: inspect(connection, profile, True, output)
    backend.collect_session = collect_session
    backend.command = command
    backend.tune_rx = tune_rx
    backend.verify_ssh = lambda connection: Client(connection).request('metadata')
    return backend

# End of file: compat.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
