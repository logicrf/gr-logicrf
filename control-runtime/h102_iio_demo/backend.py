"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       backend.py
@brief      Manage H102 identity, IIO configuration, bounded RX sessions and native recovery.
@details    Reuse the released libiio 0.x, SSH and board owner interfaces. Each channel owns a buffer/thread.
            Four analysis blocks form a bounded queue; a full queue waits at most 200 ms, then fails without dropping blocks.
            Revoke permission after destroying buffers; check native main/post receipts and report recovery failures.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.8.0
@date       2026-10-15
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ctypes as C
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import inspect as source_inspect
import json
from pathlib import Path
from queue import Queue, Full, Empty
import re
import shlex
import sys
from threading import Event, Lock
import time
import uuid

import paramiko

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
from run_h102_sdk1_iio_rx import Board, Control
from h102_iio_finite import FiniteIIO
from h102_iio_rx_continuous import ContinuousRX
from . import board_radio
from .session_link import SessionLink
from .model import budget, decode_iq, display_frame, rates, RampChecker, save_json

LEASE = "/usr/local/lib/h102/sdk1/scripts/h102_iio_rx_lease.py"
CATALOG = "/usr/local/lib/h102-config-sdk1/profiles"


BOARD_SOURCE_BYTES = Path(board_radio.__file__).read_bytes()
DESCRIBE_SOURCE = source_inspect.getsource(board_radio.describe)
HOST_SOURCE_SHA256 = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(Path(__file__).parent.glob("*.py"))}
"""Freeze loaded source identities and board payloads; later disk edits must not relabel running code."""


@dataclass(frozen=True)
class Connection:
    """Connection identity; keep credentials in memory and exclude them from repr and records."""
    host: str
    username: str
    password: str = field(repr=False)
    known_hosts: Path

    def validate(self) -> None:
        """Require a host/IP with network address characters; reject shell expressions."""
        if not re.fullmatch(r"[A-Za-z0-9_.:\-]+", self.host):
            raise ValueError("Enter the device IP or hostname")
        if not self.username or not self.password:
            raise ValueError("Enter the device username and password")


class UnknownHostKey(Exception):
    """An unrecognized device public key requiring confirmation; contains no password."""
    def __init__(self, host: str, key: paramiko.PKey):
        self.host, self.key = host, key
        self.fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        super().__init__(f"Unregistered device host key: {self.fingerprint}")


class AskHostKey(paramiko.MissingHostKeyPolicy):
    """Request first-key confirmation before authentication; never silently accept or replace keys."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


def trust_key(connection: Connection, error: UnknownHostKey) -> None:
    """Append a GUI-confirmed key; refuse to overwrite a different registered key."""
    p = connection.known_hosts
    p.parent.mkdir(parents=True, exist_ok=True)
    keys = paramiko.HostKeys()
    if p.exists():
        keys.load(str(p))
    if keys.lookup(error.host) and not keys.check(error.host, error.key):
        raise ValueError("A different device public key is registered; overwrite refused")
    keys.add(error.host, error.key.get_name(), error.key)
    temporary = p.with_suffix(".tmp")
    keys.save(str(temporary))
    temporary.chmod(0o600)
    temporary.replace(p)


def open_verified_ssh(connection: Connection):
    """Open SSH with strict host-key verification; transfer ownership on success and close on error."""
    connection.validate()
    client = paramiko.SSHClient()
    if connection.known_hosts.exists():
        client.load_host_keys(str(connection.known_hosts))
    client.set_missing_host_key_policy(AskHostKey())
    try:
        client.connect(connection.host, username=connection.username, password=connection.password,
                       look_for_keys=False, allow_agent=False, timeout=8, auth_timeout=8, banner_timeout=8)
        return client
    except Exception:
        client.close()
        raise


def verify_ssh(connection: Connection) -> None:
    """Verify credentials and the pinned key while preserving the separate command contract."""
    open_verified_ssh(connection).close()


def board_for(connection: Connection) -> Board:
    """Create a single-command SSH client with verified host key; never serialize credentials."""
    return Board(connection.host, connection.username, connection.password, connection.known_hosts)


def command(board: Board, text: str, boot: str | None = None, timeout: int = 40) -> dict:
    """Execute a predefined root request, reject boot changes and retain complete failure receipts."""
    if boot:
        text = ("python3 -B -c " + shlex.quote("from pathlib import Path;assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==" + repr(boot))
                + " || exit 125\n" + text)
    result = board.run(text, timeout=timeout)
    if result["exit"]:
        raise RuntimeError("Device operation failed: " + result["stderr"].strip()[-700:])
    return result


def inspect(connection: Connection, profile: str | None = None, prepare_control=False, control_output=None) -> dict:
    """Read the actual catalog, active parameters and board idle state after connection/selection."""
    if prepare_control and control_output is not None:
        return inspect_prepared(connection, profile, control_output)
    verify_ssh(connection)
    control = Control(connection.host)
    try:
        available = control.read("available").split()
        if len(available) != len(set(available)) or not available:
            raise RuntimeError("Invalid profile catalog")
        if profile is not None:
            if profile not in available:
                raise ValueError("The selected profile is not available on the device")
            info = control.select(profile)
        else:
            # The broker restores active_info only on its next check after native ownership is released.
            # Retry temporary ENODATA within a fixed deadline; reject persistently unknown state.
            until = time.monotonic() + 20
            while True:
                try:
                    info = json.loads(control.read("active_info"))
                    break
                except OSError as error:
                    if error.errno != 61 or time.monotonic() >= until:
                        raise
                    time.sleep(.2)
        if info.get("api") != "68.21.6":
            raise RuntimeError("This demo requires H102 SDK1/API68.21.6")
        describe_source = DESCRIBE_SOURCE
        code = """from pathlib import Path
from contextlib import ExitStack
import json,sys,os,time
sys.path.insert(0,'/usr/local/lib/h102/sdk1/scripts')
import h102_profile_config as c
from h102_iio_owner_lock import configuration_lock
c.configure_stack('sdk1-candidate')
""" + describe_source + """
for attempt in range(6):
 try:
  with ExitStack() as stack:
   stack.enter_context(configuration_lock())
   identity=c.identity();hw=c.hardware_context(stack,Path('/run'),identity);hw.idle()
   c.private(c.STATE);state=c.read_json(c.STATE)
   package=c.load_package(c.CATALOG,state['profile_id'],trusted=True)
   actual=c.snapshot(hw);c.verify_snapshot(actual,package)
   if state['identity']!=identity or state['actual']!=actual or state['sha256']!=package['manifest']['sha256']:raise ValueError('ACTIVE_PACKAGE_MISMATCH')
   radio=describe(hw,state,c.CATALOG)
   s=dict(identity=identity,actual=actual,data_idle=True)
  break
 except BlockingIOError:
  if attempt==5:raise RuntimeError('Device is configuring or receiving; stop streaming and retry')
  time.sleep(.2)
print(json.dumps(dict(identity=s['identity'],parameters=s['actual']['parameters'],radio=radio,data_idle=s['data_idle'],kernel=os.uname().release,image=json.loads(Path('/etc/h102/sdk1-image.json').read_bytes()))))
"""
        raw = command(board_for(connection), "python3 -B - <<'IDENTITY'\n" + code + "IDENTITY", info["boot_id"])
        actual = json.loads(raw["stdout"])
        if actual["parameters"] != info["parameters"]:
            raise RuntimeError("Actual profile readback differs from the active record")
        info["radio"] = actual["radio"]
        result={"available": available, "info": info, "actual": actual}
        if prepare_control:
            groups=[g for g in info['radio']['groups'] if g['editable']]
            if control_output is not None and groups:
                result['_idle_link']=prepare_idle_control(connection,info,groups[0]['channels'],control_output)
            else:result['_warm_link']=warm_control(connection,info)
        return result
    finally:
        control.close()


def inspect_prepared(connection, profile, output):
    """Return a connection snapshot from one complete locked actor check; reuse verified SSH until post.

    Software IIO provides a candidate identity; the actor verifies boot, API, all startup hashes, package and parameters,
    then discovers LO groups under the same locks. Close SSH on failure; return ownership through idle_link on success.
    """
    started=time.monotonic()
    client=open_verified_ssh(connection)
    control=None;link=None
    timings={'ssh_verified_seconds':time.monotonic()-started}
    try:
        control=Control(connection.host)
        available=control.read('available').split()
        if not available or len(available)!=len(set(available)):raise RuntimeError('Invalid profile catalog')
        if profile is not None:
            if profile not in available:raise ValueError('The selected profile is not available on the device')
            info=control.select(profile)
        else:
            until=time.monotonic()+20
            while True:
                try:
                    info=json.loads(control.read('active_info'));break
                except OSError as error:
                    if error.errno!=61 or time.monotonic()>=until:raise
                    time.sleep(.2)
        if info.get('api')!='68.21.6':raise RuntimeError('This demo requires H102 SDK1/API68.21.6')
        timings['iio_info_seconds']=time.monotonic()-started
        link=prepare_idle_control(connection,info,[1],output,client,True)
        snapshot=link.ready['connection_snapshot']
        if snapshot['parameters']!=info['parameters'] or snapshot['identity']['boot_id']!=info['boot_id']:
            raise RuntimeError('Connection precheck readback differs from the active record')
        info=deepcopy(info);info['radio']=snapshot['radio']
        link.info=deepcopy(info);link.channels=tuple(link.ready['channels'])
        timings['total_seconds']=time.monotonic()-started
        return dict(available=available,info=info,actual=snapshot,_idle_link=link,connection_timings=timings)
    except Exception:
        if link is not None:close_idle_control(link)
        else:client.close()
        raise
    finally:
        if control is not None:control.close()


def warm_control(connection,info):
    """Warm the RX control channel in a worker without taking board locks or granting RX permission."""
    link=SessionLink(connection,info,(1,),'live','normal',board_for(connection))
    try:return link.warm()
    except Exception:
        link.close(stop=False);raise


def prepare_idle_control(connection,info,channels,output,verified_client=None,discover_radio=False):
    """Prepare exclusive frequency control before connection completes; do not capture, tune or enable TX.

    output must be a new evidence directory. Release the created owner on preparation/save failure. Ordinary
    inspect does not call this by default; the existing transport-only warmup interface stays compatible.
    """
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    link=SessionLink(connection,info,tuple(channels),'idle_tune','normal',board_for(connection))
    link.client=verified_client;link.discover_radio=discover_radio
    link.idle_evidence=output;started=time.monotonic()
    record=dict(status='RUNNING',info=deepcopy(info),channels=list(channels),
        board_directory=link.directory,RF_TX=False,data_buffers_created=False,
        host_source_sha256=HOST_SOURCE_SHA256)
    try:
        save_json(output/'request.json',record)
        link.start()
        record.update(status='PASS_IDLE_CONTROL_READY',ready=link.ready,
            preparation_seconds=time.monotonic()-started,control_timings=link.timings,
            recovery_confirmed=None,recovery_state='DEFERRED_ACTIVE_CONTROL')
        save_json(output/'result.json',record)
        return link
    except Exception as error:
        record.update(status='FAIL_IDLE_CONTROL_PREPARE',error=str(error))
        try:
            if link.active:close_idle_control(link)
            else:link.close(stop=False)
        except Exception as recovery:
            record['recovery_error']=str(recovery)
            raise IdleControlRecoveryError('Independent cleanup after connection preparation is unconfirmed: '+str(output)) from error
        finally:
            try:save_json(output/'failure.json',record)
            except OSError:pass
        raise


def tune_rx(connection: Connection, info: dict, channels: list[int], frequency: int, output: Path) -> dict:
    """Tune RX only while stopped; retain temporary root module and main/post receipts, raising on failure.

    The host does not change the installed image. Each board transaction rechecks identity, profile, LO groups and original settings.
    Successful readback qualifies the control interface only, without claiming physical RF frequency accuracy.
    """
    if type(frequency) is not int or not board_radio.MIN_HZ <= frequency <= board_radio.MAX_HZ:
        raise ValueError("Center frequency must be integer Hz in 30-6000 MHz")
    group = next((g for g in info["radio"]["groups"] if g["channels"] == channels), None)
    if not group or not group["editable"]:
        raise ValueError("This LO cannot be tuned independently of TX")
    output.mkdir(parents=True, exist_ok=False)
    directory = "/run/h102-gui-rx-tune-" + uuid.uuid4().hex
    request = dict(info=info, channels=channels, frequency_hz=frequency, board_directory=directory)
    save_json(output / "request.json", request)
    verify_ssh(connection)
    source = base64.b64encode(BOARD_SOURCE_BYTES).decode()
    arguments = base64.b64encode(json.dumps(request).encode()).decode()
    code = """from pathlib import Path
import base64,json,importlib.util
r=json.loads(base64.b64decode(ARGUMENTS));p=Path(r['board_directory']);p.mkdir(mode=0o700)
f=p/'board_radio.py';f.write_bytes(base64.b64decode(SOURCE));f.chmod(0o600)
spec=importlib.util.spec_from_file_location('board_radio',f);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
print(json.dumps(m.manager(p,r['info'],r['channels'],r['frequency_hz'])))
""".replace("ARGUMENTS", repr(arguments)).replace("SOURCE", repr(source))
    native = board_for(connection).run("python3 -B - <<'TUNE'\n" + code + "TUNE", timeout=240)
    save_json(output / "native.json", native)
    if native["exit"]:
        raise RuntimeError("Tuning failed: " + native["stderr"][-500:] + "; records: " + str(output))
    result = json.loads(native["stdout"])
    save_json(output / "result.json", result)
    if not result["passed"]:
        raise RuntimeError("RX tuning failed; independent rollback records: " + str(output))
    fresh = inspect(connection)
    fresh["tune_result_directory"] = str(output)
    return fresh


class IdleControlRecoveryError(RuntimeError):
    """Independent idle-control cleanup is unconfirmed; further hardware operations must stop."""


def close_idle_control(link):
    """Release a persistent idle-control transaction and save independent post; stop further work on failure."""
    manager=link.close();native=(manager or {}).get('records',{});post=native.get('post.json',{})
    directory=getattr(link,'idle_evidence',None)
    if directory:save_json(directory/'control-end.json',dict(manager=manager,records=native))
    if not (manager and manager.get('returncode')==0 and post.get('passed')
            and post.get('configuration_unchanged') and post.get('settings_restored')):
        raise IdleControlRecoveryError('Idle-control cleanup is unconfirmed; records: '+str(directory))
    return post


def tune_rx_idle(connection, state, channels, frequency, output, prepared_link=None, keep_control=False):
    """Reuse native RX control ownership for idle tuning without an IQ buffer or another complete inspection.

    The first actor performs full checks; commands verify current profile and readback. keep_control=True retains
    the owner and explicitly records pending independent cleanup. Save post when control closes or RX ends.
    By default wait for independent post before returning. Preserve failures without retry; warmup grants no permission.
    """
    connection.validate();info=state['info']
    if type(frequency) is not int or not board_radio.MIN_HZ<=frequency<=board_radio.MAX_HZ:
        raise ValueError('Center frequency must be integer Hz in 30-6000 MHz')
    if not any(g['editable'] and g['channels']==channels for g in info['radio']['groups']):
        raise ValueError('This LO cannot be tuned independently of TX')
    output.mkdir(parents=True,exist_ok=False);started=time.monotonic()
    candidate=prepared_link
    prepared=(type(candidate) is SessionLink and candidate.connection is connection
              and not candidate.done.is_set() and (not candidate.active or candidate.mode=='idle_tune')
              and candidate.info['boot_id']==info['boot_id']
              and candidate.info['configuration_id']==info['configuration_id'])
    if prepared:
        link=candidate;link.info,link.channels,link.mode,link.pattern=info,tuple(channels),'idle_tune','normal'
    else:
        if type(candidate) is SessionLink and candidate.active:
            close_idle_control(candidate)
            raise IdleControlRecoveryError('Existing control state is invalid; reconnect before tuning')
        if type(candidate) is SessionLink and not candidate.active:candidate.close(stop=False)
        link=SessionLink(connection,info,tuple(channels),'idle_tune','normal',board_for(connection))
    record=dict(status='RUNNING',info=deepcopy(info),channels=channels,frequency_hz=frequency,
        prepared_control_used=prepared,board_directory=link.directory,data_buffers_created=False,
        host_source_sha256=HOST_SOURCE_SHA256,RF_TX=False)
    failure=None;manager=None;reply=None;kept=False;fresh=None
    try:
        save_json(output/'request.json',record)
        if not link.active:link.start()
        record['ready_seconds']=time.monotonic()-started
        reply=link.tune(frequency,channels);record['applied_seconds']=time.monotonic()-started
        save_json(output/'reply.json',reply)
        if any(reply['radio']['rx_center_frequency_hz'][str(c)]!=frequency for c in channels):
            raise RuntimeError('IDLE_TUNE_REPLY_FREQUENCY_MISMATCH')
        if keep_control:
            if not hasattr(link,'idle_evidence'):link.idle_evidence=output
            record.update(status='PASS_IDLE_TUNE_APPLIED',recovery_confirmed=None,
                recovery_state='DEFERRED_ACTIVE_CONTROL',control_evidence_directory=str(link.idle_evidence),
                total_seconds=time.monotonic()-started,control_timings=dict(link.timings))
            fresh=deepcopy({k:v for k,v in state.items() if k not in ('_idle_link','_warm_link')})
            fresh['info']['radio']=reply['radio'];fresh['actual']['radio']=reply['radio']
            fresh['_idle_link']=link;fresh['tune_result_directory']=str(output);fresh['tune_seconds']=record['total_seconds']
            save_json(output/'result.json',record)
            link.info=fresh['info'];kept=True
    except Exception as error:failure=error;record['error']=str(error)
    finally:
        # Retain the connection only after saving results and transferring ownership; release on logging/copy failure.
        if not kept:
            try:
                manager=link.close();native=(manager or {}).get('records',{})
                save_json(output/'board-records.json',native)
                if hasattr(link,'idle_evidence'):save_json(link.idle_evidence/'control-end.json',dict(manager=manager,records=native))
                post=native.get('post.json',{})
                record['recovery_confirmed']=bool(manager and post.get('passed') and post.get('configuration_unchanged')
                    and post.get('settings_restored') and post.get('frequency_retained'))
                if manager and manager.get('returncode') and failure is None:failure=RuntimeError('IDLE_TUNE_NATIVE_FAILED')
                if not record['recovery_confirmed'] and failure is None:failure=IdleControlRecoveryError('IDLE_TUNE_RECOVERY_UNCONFIRMED')
            except Exception as error:failure=failure or error;record['recovery_confirmed']=False
            record.update(status='FAIL_IDLE_TUNE' if failure else 'PASS_IDLE_TUNE',
                          total_seconds=time.monotonic()-started,control_timings=dict(link.timings))
            if failure:record['error']=str(failure)
            save_json(output/'result.json',record)
    if kept:return fresh
    if failure:
        error_class=IdleControlRecoveryError if not record.get('recovery_confirmed') else RuntimeError
        raise error_class('Idle tuning failed: '+str(failure)+'; records: '+str(output)) from failure
    radio=native['post.json']['radio']
    if radio!=reply['radio'] or any(radio['rx_center_frequency_hz'][str(c)]!=frequency for c in channels):
        record.update(status='FAIL_IDLE_TUNE',error='IDLE_TUNE_FINAL_RADIO_MISMATCH');save_json(output/'result.json',record)
        raise RuntimeError('Final tuned frequencies disagree; records: '+str(output))
    fresh=deepcopy({k:v for k,v in state.items() if k not in ('_idle_link','_warm_link')})
    fresh['info']['radio']=radio;fresh['actual']['radio']=radio
    fresh['tune_result_directory']=str(output);fresh['tune_seconds']=record['total_seconds']
    return fresh


from .recorder import IQRecorder


class RxSession:
    """RX-only session with one fixed profile and board permission spanning receive/analysis threads.

    A single worker calls run; the UI only calls stop/snapshot. Hardware pointers never cross threads.
    Cache at most one complete raw block per channel. A full four-block analysis queue fails after at most 200 ms.
    """
    def __init__(self, connection: Connection, info: dict, channels: tuple[int, ...],
                 mode: str, pattern: str, seconds: int | None, output: Path, event=None, prepared_link=None, recording=False):
        if mode not in ("continuous", "snapshot", "live") or pattern not in ("normal", "ramp16"):
            raise ValueError("Invalid receive mode")
        if mode == "continuous" and (type(seconds) is not int or not 1 <= seconds <= 300):
            raise ValueError("Timed session duration must be 1-300 seconds")
        if recording and mode not in ("live", "continuous"):
            raise ValueError("Recording requires live reception")
        self.recording = recording
        self.recorder = None
        self.guard_lock = Lock()
        self.fs = rates(info, channels)
        if mode in ("continuous", "live") and not budget(info, channels).allowed:
            raise ValueError("Selected channels exceed the continuous network IIO budget; choose a finite snapshot")
        self.connection, self.info, self.channels = connection, info, channels
        self.mode, self.pattern, self.seconds, self.output = mode, pattern, seconds, output
        self.event = event or (lambda kind, value: None)
        self.stopping, self.closing = Event(), Event()
        self.lock = Lock()
        self.latest: dict[int, bytes] = {}
        self.frames: dict[int, dict] = {}
        self.stats: dict[int, dict] = {}
        self.directory = "/run/h102-iio-rx-" + uuid.uuid4().hex
        self.board = board_for(connection)
        self.watch = None
        self.deadline = 0.0
        self.link = None
        self.prepared_link = prepared_link
        self.epoch_stop = Event()
        self.pending_tune = None
        self.epoch = 0
        self.started = 0.0
        self.timings = {}
        self.capture_deadline = None
        self.epochs = []

    def stop(self) -> None:
        """Cancel this host receive operation; run's finally closes buffers and issues board STOP."""
        self.stopping.set()

    def snapshot(self) -> tuple[dict[int, bytes], dict, dict]:
        """Return immutable raw blocks and statistics/plot snapshots without hardware or credential references."""
        with self.lock:
            return dict(self.latest), {k: dict(v) for k, v in self.stats.items()}, dict(self.frames)

    def mark(self, stage):
        """Record each stage's first host-monotonic elapsed time; never mix in board wall-clock time."""
        with self.lock:
            self.timings.setdefault(stage, time.monotonic() - self.started)

    def can_tune(self):
        """Allow one manual tuning request for the same ready owner while neither stopping nor tuning."""
        return (not self.recording and self.link is not None and self.link.permitted() and not self.stopping.is_set()
                and not self.closing.is_set() and not self.epoch_stop.is_set())

    def request_tune(self, channels, frequency):
        """End the current epoch without SSH/PLL work in Qt; the gap is excluded from continuous samples."""
        groups=self.info['radio']['groups']
        if (type(frequency) is not int or not board_radio.MIN_HZ <= frequency <= board_radio.MAX_HZ
                or not any(g['editable'] and g['channels']==list(channels) for g in groups)):
            raise ValueError('Invalid tuning frequency or LO group')
        with self.lock:
            if not self.can_tune() or self.pending_tune is not None:raise RuntimeError('The current session cannot tune')
            self.pending_tune=dict(channels=list(channels),frequency_hz=frequency,requested_monotonic=time.monotonic())
            self.epoch_stop.set()
        self.event('retuning',dict(frequency_hz=frequency))

    def snapshot_with_info(self):
        """Read IQ and immutable epoch identity under one lock to avoid mislabelling exports across tuning."""
        with self.lock:
            return (dict(self.latest), {k:dict(v) for k,v in self.stats.items()},
                    dict(self.frames),deepcopy(self.info),self.epoch)

    def permitted(self) -> bool:
        """Check timed-mode deadlines; live mode checks fresh health from the same native actor without restart."""
        if self.link is not None:
            return not self.closing.is_set() and self.link.permitted()
        return not self.closing.is_set() and time.monotonic() < self.deadline and self.watch is not None and not self.watch.done()

    def _receive(self, channel: int) -> dict:
        """One thread owns the actual context/buffer; destroy buffers on every return path."""
        fs = self.fs[channel]
        epoch_info, epoch = self.info, self.epoch
        block_samples = 65536 if fs < 1_000_000 else 262144
        queue: Queue = Queue(maxsize=4)
        reader_done = Event()
        reader_errors: list[Exception] = []
        checker = RampChecker()
        digest = hashlib.sha256()
        last_raw = None
        first_received = None
        last_received = None
        result = {"channel": channel, "samples": 0, "bytes": 0, "blocks": 0,
                  "within_errors": 0, "boundary_errors": 0, "iq_errors": 0,
                  "timeouts": 0, "mode": self.mode, "source": self.pattern,
                  "continuity": "unknown" if self.pattern == "normal" else "modulo65536",
                  "absolute_continuity_proven": False, "block_samples": 65536 if self.mode == "snapshot" else block_samples,
                  "analysis_backpressure_events": 0, "analysis_backpressure_seconds": 0.0}
        result.update(context_creations=0, buffer_creations=0,epoch=epoch,
                      center_hz=epoch_info['radio']['rx_center_frequency_hz'][str(channel)])

        def reader():
            """Copy complete buffers only; wait through bounded scheduling jitter and fail persistent overload without dropping blocks."""
            try:
                if self.mode == "snapshot":
                    with FiniteIIO("ip:" + self.connection.host, self.permitted) as client:
                        result["library_version"] = client.version
                        raw, geometry = client.capture(channel)
                        queue.put((raw, time.monotonic(), geometry), timeout=1)
                    return
                with ContinuousRX("ip:" + self.connection.host, self.permitted) as client:
                    result["context_creations"] += 1
                    self.mark(f"rx{channel}_context_seconds")
                    result["library_version"] = client.version
                    device, scan = client._device(channel)
                    count = 4 if fs < 1_000_000 else 16
                    if client.lib.iio_device_set_kernel_buffers_count(device, count) < 0:
                        raise RuntimeError("Cannot configure receive buffers")
                    buffer = None
                    try:
                        buffer = client.lib.iio_device_create_buffer(device, block_samples, False)
                        if not buffer:
                            raise RuntimeError("Cannot create the receive buffer")
                        result["buffer_creations"] += 1
                        self.mark(f"rx{channel}_buffer_seconds")
                        warming = count*2+2 if self.recording else 0
                        if self.recording:
                            with self.guard_lock:result['guard_warmup']=self.link.guard('begin',channel)
                        with self.lock:
                            if self.mode == 'continuous' and self.capture_deadline is None and not warming:
                                self.capture_deadline=time.monotonic()+self.seconds
                        end_time = float('inf') if self.mode == "live" or warming else self.capture_deadline
                        discard = epoch > 0  # Discard one complete block after tuning; do not check continuity across the gap.
                        while not self.stopping.is_set() and not self.epoch_stop.is_set() and time.monotonic() < end_time:
                            if not self.permitted():
                                raise RuntimeError("Device receive permission has expired")
                            size = client.lib.iio_buffer_refill(buffer)
                            if size < 0:
                                if self.stopping.is_set():
                                    break
                                if -size in (110, 116):
                                    result["timeouts"] += 1
                                raise OSError(-size, "IIO receive timeout or transfer failure")
                            base = client.lib.iio_buffer_start(buffer)
                            positions = [client.lib.iio_buffer_first(buffer, entry) - base for entry in scan]
                            if (size != block_samples * 4 or client.lib.iio_buffer_step(buffer) != 4
                                    or positions != [0, 2] or client.lib.iio_buffer_end(buffer) - base != size):
                                raise RuntimeError("Invalid IQ buffer layout or length")
                            self.mark(f"rx{channel}_first_refill_seconds")
                            if warming:
                                warming-=1
                                if not warming:
                                    with self.guard_lock:result['guard_start']=self.link.guard('arm',channel)
                                    with self.lock:
                                        if self.mode=='continuous' and self.capture_deadline is None:
                                            self.capture_deadline=time.monotonic()+self.seconds
                                        end_time=float('inf') if self.mode=='live' else self.capture_deadline
                                    result['warmup_discarded_blocks']=count*2+2
                                continue
                            if discard:
                                discard=False
                                continue
                            raw = C.string_at(base, size)
                            if self.recorder is not None:
                                self.recorder.submit(channel, raw)
                            try:
                                queue.put_nowait((raw, time.monotonic(), {"samples": block_samples, "scan_order": ["I", "Q"]}))
                            except Full:
                                received = time.monotonic()
                                result["analysis_backpressure_events"] += 1
                                try:
                                    queue.put((raw, received, {"samples": block_samples, "scan_order": ["I", "Q"]}), timeout=.2)
                                except Full:
                                    raise RuntimeError("IQ analysis queue overloaded for 200 ms; acquisition stopped")
                                finally:
                                    result["analysis_backpressure_seconds"] += time.monotonic() - received
                    finally:
                        if buffer:
                            try:
                                if self.recording and result.get('guard_start') and not reader_errors:
                                    with self.guard_lock:result['device_guard']=self.link.guard('end',channel)
                            finally:
                                client.lib.iio_buffer_destroy(buffer)
                        if client.lib.iio_device_set_kernel_buffers_count(device, 4) < 0:
                            raise RuntimeError("Cannot restore receive buffer settings")
            except Exception as error:
                reader_errors.append(error)
                self.stopping.set()
            finally:
                reader_done.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reader)
            displayed = 0.0
            try:
                while not reader_done.is_set() or not queue.empty():
                    try:
                        raw, received, geometry = queue.get(timeout=.2)
                    except Empty:
                        continue
                    a = decode_iq(raw)
                    digest.update(raw)
                    last_raw = raw
                    result["samples"] += len(a)
                    result["bytes"] += len(raw)
                    result["blocks"] += 1
                    if first_received is None:
                        first_received = received
                    last_received = received
                    elapsed = received - first_received
                    measured = (result["bytes"] - len(raw)) / elapsed if elapsed > 0 else 0
                    result.update(elapsed=elapsed, bytes_per_second=measured,
                                  first_received_monotonic=first_received,last_received_monotonic=received)
                    if self.pattern == "ramp16":
                        result.update(checker.check(raw))
                    with self.lock:
                        self.latest[channel] = raw
                        self.stats[channel] = dict(result)
                    if self.pattern == "ramp16" and (checker.within or checker.boundary or checker.pair):
                        raise RuntimeError(f"RX{channel} digital continuity check failed")
                    if received - displayed >= .03:
                        frame = display_frame(raw, fs)
                        frame.update(center_hz=result['center_hz'],epoch=epoch)
                        self.mark(f"rx{channel}_first_frame_seconds")
                        with self.lock:
                            self.frames[channel] = frame
                        displayed = received
                future.result()
                if reader_errors:
                    raise reader_errors[0]
            except Exception as exc:
                result["error"] = str(exc)
                self.stopping.set()
                raise
            finally:
                future.result()
                result["nominal_rate_ratio"] = result.get("bytes_per_second", 0) / (fs * 4)
                result["stream_sha256"] = digest.hexdigest()
                if last_raw is not None:
                    last_path = self.output / f"rx{channel}-last.bin"
                    last_path.write_bytes(last_raw)
                    last_path.chmod(0o600)
                    result["last_block_sha256"] = hashlib.sha256(last_raw).hexdigest()
                with self.lock:
                    self.stats[channel] = dict(result)
                save_json(self.output / f"rx{channel}-result.json", result)
        return result

    def run(self) -> dict:
        """Run and save main/post; recovery failure always returns FAIL so the UI cannot mark success."""
        if self.mode in ("live", "continuous"):
            return self._run_live()
        self.started=time.monotonic()
        self.output.mkdir(parents=True, exist_ok=False)
        record = {"status": "RUNNING", "info": self.info, "channels": self.channels,
                  "mode": self.mode, "pattern": self.pattern, "requested_seconds": self.seconds,
                  "board_directory": self.directory, "RF_TX": False, "started_at": datetime.now(timezone.utc).isoformat(),
                  "host_python": sys.version.split()[0],
                  "host_source_sha256": HOST_SOURCE_SHA256}
        save_json(self.output / "request.json", record)
        manager = watch = None
        ready = None
        error = None
        with ThreadPoolExecutor(max_workers=4) as executor:
            try:
                lease = 330 if self.mode == "continuous" else 30
                text = shlex.join(["python3", "-B", LEASE, "manager", "--catalog", CATALOG,
                                   "--directory", self.directory, "--pattern", self.pattern, "--seconds", str(lease)])
                boot_check = "python3 -B -c " + shlex.quote("from pathlib import Path;assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==" + repr(self.info["boot_id"]))
                text = boot_check + " || exit 125\n" + text
                manager = executor.submit(self.board.run, text, timeout=lease + 45)
                until = time.monotonic() + 20
                while not ready:
                    r = self.board.run("test -f " + self.directory + "/ready.json && cat " + self.directory + "/ready.json", timeout=10)
                    if r["exit"] == 0:
                        ready = json.loads(r["stdout"])
                        break
                    if manager.done():
                        save_json(self.output / "manager-early.json", manager.result())
                        raise RuntimeError("Device receive preparation failed or device is busy")
                    if time.monotonic() >= until:
                        raise TimeoutError("Device receive preparation timed out")
                    time.sleep(.1)
                code = """from pathlib import Path
import json,sys,time
sys.path.insert(0,'/usr/local/lib/h102/sdk1/scripts')
import h102_iio_rx_lease as rx
rx.config.configure_stack('sdk1-candidate');p=Path(SESSION)
expected_carriers=EXPECTED_CARRIERS
def carrier_check(hw):
 actual={str(i+1):int(hw.read(hw.alt('adrv9002-phy',i,'frequency'))) for i in (0,1)}
 if actual!=expected_carriers:raise RuntimeError('RX_CENTER_FREQUENCY_CHANGED')
r=rx.inputs(p)
assert r['identity']['boot_id']==EXPECTED_BOOT and r['configuration_id']==EXPECTED_CONFIG
hw=rx.hardware(p,r);v=rx.permit(p,hw=hw);carrier_check(hw)
rx.atomic_save(p/'gui-watch-ready.json',v,exclusive=True)
while not (p/'stop').exists():
 try:
  carrier_check(hw);print(json.dumps(rx.permit(p,hw=hw)),flush=True)
 except Exception:
  if (p/'stop').exists():break
  raise
 time.sleep(.5)
""".replace("SESSION", repr(self.directory)).replace("EXPECTED_BOOT", repr(self.info["boot_id"])).replace("EXPECTED_CONFIG", repr(self.info["configuration_id"])).replace("EXPECTED_CARRIERS", repr(self.info["radio"]["rx_center_frequency_hz"]))
                self.watch = watch = executor.submit(self.board.run, "python3 -B - <<'WATCH'\n" + code + "WATCH", timeout=lease + 20)
                until = time.monotonic() + 10
                while True:
                    r = self.board.run("test -f " + self.directory + "/gui-watch-ready.json && cat " + self.directory + "/gui-watch-ready.json", timeout=10)
                    if r["exit"] == 0:
                        proof = json.loads(r["stdout"])
                        break
                    if watch.done() or time.monotonic() >= until:
                        raise RuntimeError("Device receive state verification failed")
                    time.sleep(.1)
                if proof["configuration_id"] != self.info["configuration_id"]:
                    raise RuntimeError("Device profile changed before startup")
                self.deadline = time.monotonic() + (proof["ready"]["deadline_ns"] - proof["now_ns"]) / 1e9 - 2
                save_json(self.output / "ready.json", proof)
                self.event("ready", {"output": str(self.output)})
                if not self.stopping.is_set():
                    received = []
                    # Over-budget snapshots create buffers serially; the channels are not claimed to be time-aligned.
                    futures = [] if self.mode == "snapshot" else [executor.submit(self._receive, c) for c in self.channels]
                    if self.mode == "snapshot":
                        for channel in self.channels:
                            if not self.stopping.is_set():
                                received.append(self._receive(channel))
                    for f in futures:
                        try:
                            received.append(f.result())
                        except Exception as exc:
                            error = error or exc
                            self.stopping.set()
                    record["results"] = received
                    if error:
                        raise error
                record["status"] = "PASS_RX_SESSION"
            except Exception as exc:
                error = exc
                self.stopping.set()
                record.update(status="FAIL_RX_SESSION", error=str(exc))
            finally:
                self.closing.set()
                if manager is not None:
                    try:
                        stop_command = shlex.join(["python3", "-B", LEASE, "stop", "--directory", self.directory])
                        # Revoke requests from this random directory only, including after preparation failures.
                        stopped = command(self.board, "if test -f " + self.directory + "/request.json; then " + stop_command + "; fi", self.info["boot_id"])
                        save_json(self.output / "stop.json", stopped)
                    except Exception as exc:
                        record.update(status="FAIL_RX_SESSION", stop_error=str(exc))
                if watch is not None:
                    try:
                        w = watch.result(timeout=20)
                        save_json(self.output / "watch.json", w)
                        if w["exit"]:
                            record.update(status="FAIL_RX_SESSION", watch_error=w["stderr"][-700:])
                    except Exception as exc:
                        record.update(status="FAIL_RX_SESSION", watch_error=str(exc))
                if manager is not None:
                    try:
                        m = manager.result(timeout=lease + 45 if ready is None else 50)
                        save_json(self.output / "manager.json", m)
                        if m["exit"]:
                            record.update(status="FAIL_RX_SESSION", manager_error=m["stderr"][-700:])
                        collect = "python3 -B - <<'COLLECT'\nfrom pathlib import Path\nimport json\np=Path(" + repr(self.directory) + ")\nprint(json.dumps({n:json.loads((p/n).read_text()) for n in ['request.json','main.json','post.json','manager.json'] if (p/n).exists()}))\nCOLLECT"
                        collected = command(self.board, collect, self.info["boot_id"])
                        raw = json.loads(collected["stdout"])
                        save_json(self.output / "board-records.json", raw)
                        post = raw.get("post.json", {})
                        if not post.get("passed") or not post.get("configuration_unchanged"):
                            record.update(status="FAIL_RX_SESSION", recovery_confirmed=False)
                        else:
                            record["recovery_confirmed"] = True
                    except Exception as exc:
                        record.update(status="FAIL_RX_SESSION", recovery_confirmed=False, recovery_error=str(exc))
                self.board.password = None
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                save_json(self.output / "result.json", record)
        self.event("finished", record)
        return record

    def _run_live(self):
        """Serve live/timed RX under one native owner; retuning creates epochs and stop performs independent cleanup."""
        self.started=time.monotonic()
        self.output.mkdir(parents=True,exist_ok=False)
        record=dict(status='RUNNING',info=deepcopy(self.info),channels=self.channels,mode=self.mode,pattern=self.pattern,
            requested_seconds=self.seconds if self.mode=='continuous' else None,RF_TX=False,
            started_at=datetime.now(timezone.utc).isoformat(),host_source_sha256=HOST_SOURCE_SHA256,results=[])
        candidate=self.prepared_link
        adopted_idle=(type(candidate) is SessionLink and candidate.connection is self.connection
                      and candidate.active and candidate.mode=='idle_tune' and not candidate.done.is_set())
        if adopted_idle:
            self.link=candidate
        elif candidate is not None and candidate.connection is self.connection and not candidate.done.is_set() and not candidate.active:
            self.link=candidate
            self.link.info,self.link.channels,self.link.mode,self.link.pattern=self.info,self.channels,'live',self.pattern
        else:
            self.link=SessionLink(self.connection,self.info,self.channels,'live',self.pattern,self.board)
        self.directory=self.link.directory;record['board_directory']=self.directory
        save_json(self.output/'request.json',record)
        try:
            if self.recording:
                self.recorder = IQRecorder(self.output/'recording', self.channels, deepcopy(self.info),
                                           sum(self.fs.values())*4, self.seconds if self.mode=='continuous' else None, self.pattern, require_device_guard=True)
            if adopted_idle:self.link.activate_rx(self.info,self.channels,self.pattern)
            else:self.link.start()
            record['adopted_idle_control']=adopted_idle;self.mark('native_permission_seconds')
            save_json(self.output/'ready.json',self.link.ready)
            self.event('ready',dict(output=str(self.output)))
            while not self.stopping.is_set():
                received=[]
                with ThreadPoolExecutor(max_workers=len(self.channels)) as executor:
                    futures=[executor.submit(self._receive,c) for c in self.channels]
                    for future in futures:
                        try:received.append(future.result())
                        except Exception as error:
                            record.update(status='FAIL_RX_SESSION',error=str(error));self.stopping.set()
                if self.epochs and 'transition' in self.epochs[-1]:
                    old={v['channel']:v for v in self.epochs[-1]['results']}
                    self.epochs[-1]['transition']['capture_gap_seconds']={str(v['channel']):
                        v['first_received_monotonic']-old[v['channel']]['last_received_monotonic']
                        for v in received if v.get('first_received_monotonic') is not None
                        and old.get(v['channel'],{}).get('last_received_monotonic') is not None}
                self.epochs.append(dict(epoch=self.epoch,radio=deepcopy(self.info['radio']),results=received))
                self.epochs=self.epochs[-128:]
                record['results']=received
                if self.stopping.is_set():break
                with self.lock:target=self.pending_tune
                if target is None:break  # Timed session reached its normal deadline.
                if self.capture_deadline is not None and time.monotonic()>=self.capture_deadline:break
                # Permit PLL writes only after buffers close and queues drain; retain the owner and heartbeat.
                reply=self.link.tune(target['frequency_hz'],target['channels'])
                time.sleep(.1)
                with self.lock:
                    self.info=deepcopy(self.info);self.info['radio']=reply['radio']
                    self.epoch+=1
                    self.latest.clear();self.frames.clear();self.stats.clear()
                    self.pending_tune=None;self.epoch_stop.clear()
                gap=time.monotonic()-target['requested_monotonic']
                self.epochs[-1]['transition']=dict(frequency_hz=target['frequency_hz'],channels=target['channels'],
                    control_gap_seconds=gap,driver_readback=reply['radio'],physical_rf_verified=False)
                self.event('tuned',dict(info=deepcopy(self.info),epoch=self.epoch,gap_seconds=gap))
            if record['status']!='FAIL_RX_SESSION':record['status']='PASS_RX_SESSION'
        except Exception as error:
            self.stopping.set();record.update(status='FAIL_RX_SESSION',error=str(error))
        finally:
            self.closing.set();self.mark('cleanup_started_seconds')
            if self.recorder is not None:
                try:
                    record['recording'] = self.recorder.finish(record['status']=='PASS_RX_SESSION', record['results'])
                except Exception as error:
                    record.update(status='FAIL_RX_SESSION', recording_error=str(error))
            try:
                manager=self.link.close();save_json(self.output/'control-end.json',dict(manager=manager,error=str(self.link.error) if self.link.error else None))
                # The authenticated channel returns native receipts after independent ExecStopPost; no extra SSH read is needed.
                native=manager['records'] if manager and manager.get('records',{}).get('post.json') else collect_session(self.board,self.directory,self.info['boot_id'])
                save_json(self.output/'board-records.json',native)
                if hasattr(self.link,'idle_evidence'):
                    save_json(self.link.idle_evidence/'control-end.json',dict(manager=manager,records=native,handoff_rx_output=str(self.output)))
                post=native.get('post.json',{})
                record['recovery_confirmed']=bool(post.get('passed') and post.get('configuration_unchanged') and post.get('settings_restored'))
                record['final_radio']=post.get('radio')
                if not record['recovery_confirmed']:
                    record.update(status='FAIL_RX_SESSION',recovery_error='Independent recovery unconfirmed')
                elif not manager or manager['returncode']:
                    record.update(status='FAIL_RX_SESSION',native_failure=True)
            except Exception as error:record.update(status='FAIL_RX_SESSION',recovery_confirmed=False,recovery_error=str(error))
            self.mark('finished_seconds')
            record.update(epochs=self.epochs,final_info=self.info,timings=dict(self.timings),
                control_timings=dict(self.link.timings),finished_at=datetime.now(timezone.utc).isoformat())
            self.board.password=None
            save_json(self.output/'result.json',record)
        self.event('finished',record);return record


def collect_session(board,directory,boot):
    """Wait independently for post after control disconnects; never report recovery without a receipt."""
    until=time.monotonic()+95
    text="python3 -B - <<'COLLECT'\nfrom pathlib import Path\nimport json,subprocess\np=Path("+repr(directory)+")\nr={n:json.loads((p/n).read_text()) for n in ['request.json','ready.json','main.json','post.json','manager.json'] if (p/n).exists()}\nif 'request.json' in r:r['_unit_properties']=subprocess.run(['systemctl','show',r['request.json']['unit'],'-p','ActiveState','-p','Result','-p','ExecMainStatus'],capture_output=True,text=True).stdout\nprint(json.dumps(r))\nCOLLECT"
    while True:
        native=json.loads(command(board,text,boot,timeout=15)['stdout'])
        properties=native.get('_unit_properties','')
        if ('post.json' in native or not native or 'ActiveState=failed\n' in properties
                or 'ActiveState=inactive\n' in properties or time.monotonic()>=until):return native
        time.sleep(1)

# End of file: backend.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com
