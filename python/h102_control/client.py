"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file client.py
@brief Request fixed control operations/exclusive native sessions by device address without Linux credentials.
@details Ordinary requests use separate connections without retry; callers own long-lived sessions. Disconnection
         never recreates or renews them. The server accepts typed operations only, without remote code execution.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
from dataclasses import dataclass
import re
import socket
import uuid

from . import DEFAULT_PORT, PROTOCOL_VERSION
from .protocol import ProtocolError, encode_frame, receive_frame


class DeviceError(RuntimeError):
    """Explicit device rejection/operation failure; code/detail contain no authentication data."""
    def __init__(self, code, detail=''):
        self.code = str(code)
        self.detail = str(detail)[:2000]
        super().__init__(self.code + (': ' + self.detail if detail else ''))


@dataclass(frozen=True)
class Connection:
    """Ordinary connection stores address/protocol port only, without username, password or private key."""
    host: str
    port: int = DEFAULT_PORT

    def validate(self):
        """Reject empty addresses, URLs, whitespace and invalid ports before creating sockets."""
        if (not isinstance(self.host, str) or len(self.host) > 253 or
                not re.fullmatch(r'[A-Za-z0-9_.:\-]+', self.host)):
            raise ValueError('CONTROL_API_DEVICE_ADDRESS_REQUIRED')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('CONTROL_API_PORT_RANGE')


class Client:
    """Stateless single-host RPC client; thread-safe instances use separate sockets per call."""
    def __init__(self, connection):
        connection.validate()
        self.connection = connection

    def request(self, operation, arguments=None, timeout=30):
        """Send once and check version/request ID; never assume uncertain disconnected/timed-out changes succeeded."""
        identifier = uuid.uuid4().hex
        request = dict(version=PROTOCOL_VERSION, request_id=identifier,
                       operation=operation, arguments=arguments or {})
        wire = encode_frame(request)
        address = (self.connection.host, self.connection.port)
        try:
            with socket.create_connection(address, timeout=8) as peer:
                peer.settimeout(timeout)
                peer.sendall(wire)
                with peer.makefile('rb') as reader:
                    result = receive_frame(reader)
        except (OSError, EOFError) as error:
            raise DeviceError('CONTROL_API_UNAVAILABLE_OR_OUTCOME_UNKNOWN',
                              'Check that H102 device-control services are installed and read device status before retrying') from error
        self._check(result, identifier)
        return result['result']

    @staticmethod
    def _check(result, identifier):
        if result.get('version') != PROTOCOL_VERSION or result.get('request_id') != identifier:
            raise ProtocolError('CONTROL_API_RESPONSE_IDENTITY')
        if result.get('ok') is not True:
            error = result.get('error') or {}
            raise DeviceError(error.get('code', 'CONTROL_API_FAILED'), error.get('detail', ''))
        if 'result' not in result:
            raise ProtocolError('CONTROL_API_RESPONSE_RESULT_MISSING')

    def status(self):
        """Read actual idle state; the device explicitly rejects busy or unknown identity."""
        return self.request('status')

    def open_session(self, entrypoint, sources):
        """Open an installation-pinned native session by source hashes only; never upload executable source.

        Return (peer, reader, ready). The caller must close both handles. Native adapters handle subsequent
        pulse/control frames; this client never fabricates health permission in a background task.
        """
        return self.open_stream('session.open',dict(entrypoint=entrypoint,sources=sources))

    def open_stream(self, operation, arguments):
        """Open a fixed session, transferring socket ownership on success and closing all handles on failure."""
        if operation not in ('session.open','usb.open'):raise ValueError('CONTROL_API_STREAM_OPERATION')
        identifier = uuid.uuid4().hex
        value = dict(version=PROTOCOL_VERSION, request_id=identifier,
                     operation=operation, arguments=arguments)
        address = (self.connection.host, self.connection.port)
        peer = socket.create_connection(address, timeout=8)
        reader = None
        try:
            peer.settimeout(15)
            peer.sendall(encode_frame(value))
            reader = peer.makefile('rb')
            reply = receive_frame(reader)
            self._check(reply, identifier)
            return peer, reader, reply['result']
        except Exception:
            if reader:
                reader.close()
            peer.close()
            raise

# End of file: client.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
