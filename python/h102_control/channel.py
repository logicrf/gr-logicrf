"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file channel.py
@brief Adapt fixed device-session frames to SessionLink's bounded bidirectional channel.
@details No exec_command/authentication interface; forward stdout/stderr/exit status independently.
         Reader closure/protocol failure revokes transport without reconnection or synthesized pulses.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import base64
import socket
from threading import Event, Lock, Thread

from .protocol import MAX_FRAME_BYTES, ProtocolError, encode_frame, receive_frame


class SessionChannel:
    """One reader maintains bounded control buffers; a send lock serializes sendall.

    Own peer/reader and close idempotently; shutdown wakes blocked reads.
    Overflow is an actual error; never discard stdout to pretend control remains healthy.
    """
    def __init__(self, peer, reader):
        self.peer, self.reader = peer, reader
        self.lock, self.send_lock = Lock(), Lock()
        self.stdout, self.stderr = bytearray(), bytearray()
        self.exit_status, self.failure = None, None
        self.closed = Event()
        peer.settimeout(None)
        self.thread = Thread(target=self._read, daemon=True, name='h102-api-control-reader')
        self.thread.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                frame = receive_frame(self.reader)
                kind = frame.get('stream')
                if kind in ('stdout', 'stderr'):
                    data = base64.b64decode(frame['data'], validate=True)
                    with self.lock:
                        target = self.stdout if kind == 'stdout' else self.stderr
                        if len(target) + len(data) > MAX_FRAME_BYTES:
                            raise ProtocolError('CONTROL_API_SESSION_BUFFER_LIMIT')
                        target.extend(data)
                elif kind == 'exit' and type(frame.get('status')) is int:
                    with self.lock:
                        self.exit_status = frame['status']
                    return
                else:
                    raise ProtocolError('CONTROL_API_SESSION_FRAME_TYPE')
        except Exception as error:
            with self.lock:
                if not self.closed.is_set():
                    self.failure = error
                    self.stderr.extend(str(error).encode('utf-8')[:1000])
                    self.exit_status = 125

    def sendall(self, data):
        """Forward complete native JSON lines only, without shell or extra API operations."""
        if isinstance(data, str):
            data = data.encode('utf-8')
        if not data.endswith(b'\n') or len(data) > MAX_FRAME_BYTES:
            raise ProtocolError('CONTROL_API_SESSION_COMMAND_FRAME')
        from .protocol import decode_frame
        value = decode_frame(data)
        with self.send_lock:
            self.peer.sendall(encode_frame(dict(control=value)))

    def recv_ready(self):
        with self.lock:
            return bool(self.stdout)

    def recv_stderr_ready(self):
        with self.lock:
            return bool(self.stderr)

    def _take(self, buffer, count):
        with self.lock:
            data = bytes(buffer[:count])
            del buffer[:count]
            return data

    def recv(self, count):
        return self._take(self.stdout, count)

    def recv_stderr(self, count):
        return self._take(self.stderr, count)

    def exit_status_ready(self):
        with self.lock:
            return self.exit_status is not None

    def recv_exit_status(self):
        with self.lock:
            if self.exit_status is None:
                raise RuntimeError('CONTROL_API_SESSION_NOT_FINISHED')
            return self.exit_status

    def close(self):
        """Revoke local control; the device recovers through native EOF/heartbeat deadlines and independent post."""
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.peer.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.peer.close()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError('CONTROL_API_READER_NOT_JOINED')
        self.reader.close()

# End of file: channel.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
