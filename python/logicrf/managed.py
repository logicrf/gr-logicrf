"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file managed.py
@brief Connect fixed native H102 ownership to C++ data sessions without Python IQ transport.
@details Verify identity/profile at start; native heartbeats renew short local permits; every exit checks independent post.
         Construction is network-free; provider credentials remain in RAM; every evidence directory is separate.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.0
@date 2026-10-16
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time
import uuid
import weakref


def iio_frame_samples(rate,direction):
    """Keep low-rate blocks below 250 ms; high-rate RX uses 262144 samples and TX 65536."""
    count=max(1024,min(65536,1<<max(0,int(rate/4).bit_length()-1))) if rate<1e6 else 262144
    return min(count,65536) if direction=='tx' else count


def configure_native(native,profile,rate,frequency):
    """Validate fully before stopped-state parameter/block changes; never touch hardware or graph topology."""
    from .logicrf_python import device
    options=native.options();options.configuration=profile;options.sample_rate=rate;options.center_frequency=frequency
    if options.backend=='iio':options.frame_samples=iio_frame_samples(rate,options.direction)
    checked=device.make(options)
    native.configure(profile,rate,frequency)
    if options.backend=='iio':native.set_frame_samples(options.frame_samples)


SUPPORTED_RUNTIME_COMMITS = ('f782657cc911834d31bf136da5b19260e25aeea8',
    'bf274c9391577c7326ada7ad3d3578f1468ef86a')
"""Accept frozen legacy runtime and verified cap3 ADC updates only; check every file's full SHA."""


def default_runtime(project, lock_path):
    """Prefer control sources bundled with the matching module; callers may explicitly supply a runtime.

    Source/install packages carry the same runtime; customers need no private GUI repository.
    Use the historical project environment only for old unbundled installs; verify every SHA before loading.
    """
    lock=json.loads(Path(lock_path).read_text())
    if lock['gui_commit'] not in SUPPORTED_RUNTIME_COMMITS:raise RuntimeError('CONTROL_RUNTIME_PIN')
    bundled=Path(__file__).resolve().parent/'runtime'
    if bundled.is_dir():return bundled
    return Path(project)/'envs/h102-control-runtime'/lock.get('runtime_directory',lock['gui_commit'][:7])


def _verified_runtime(root, lock_path):
    """Check each pinned source file; ordinary and explicit maintenance entries share identity validation."""
    root = Path(root).resolve()
    lock = json.loads(Path(lock_path).read_text())
    if lock['gui_commit'] not in SUPPORTED_RUNTIME_COMMITS:
        raise RuntimeError('CONTROL_RUNTIME_PIN')
    for name, digest in lock['runtime_files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('CONTROL_RUNTIME_SOURCE_MISMATCH: '+name)
    return root


def load_runtime(root, lock_path):
    """Ordinary control uses the API only; construction/import never requests credentials or networking."""
    root=_verified_runtime(root,lock_path)
    if 'h102_iio_demo.backend' in sys.modules:
        found = Path(sys.modules['h102_iio_demo.backend'].__file__).resolve()
        if found != root/'h102_iio_demo/backend.py':
            raise RuntimeError('CONTROL_RUNTIME_ALREADY_LOADED_FROM_OTHER_ROOT')
    sys.path.insert(0, str(root))
    from h102_iio_demo import backend
    from h102_iio_demo import session_link
    from h102_control.compat import install
    return install(backend, session_link)


def load_maintenance_runtime(root, lock_path):
    """Separate SSH maintenance namespace for historical administrator tools, never an application fallback.

    Callers explicitly provide maintenance credentials and verify host keys; GUI/GRC/ordinary examples never call it.
    Keep it separate from the normal API adapter so one process cannot switch ordinary control back to SSH.
    """
    import importlib
    import importlib.util
    root=_verified_runtime(root,lock_path)
    alias='_logicrf_h102_maintenance'
    if alias not in sys.modules:
        spec=importlib.util.spec_from_file_location(alias,root/'h102_iio_demo/__init__.py',
                                                  submodule_search_locations=[str(root/'h102_iio_demo')])
        module=importlib.util.module_from_spec(spec);sys.modules[alias]=module
        spec.loader.exec_module(module)
    backend=importlib.import_module(alias+'.backend')
    if Path(backend.__file__).resolve()!=root/'h102_iio_demo/backend.py':
        raise RuntimeError('MAINTENANCE_RUNTIME_ALREADY_LOADED_FROM_OTHER_ROOT')
    return backend


class ManagedDevice:
    """Own the user-facing Device and Qt-free controller; retain this object throughout graph lifetime.

    Native callbacks retain the controller, which weakly references Native to avoid Python/C++ cycles.
    Call close after graph.stop/wait and check error/recovery on exit.
    """
    def __init__(self, native, controller):
        self.native = native
        self.controller = controller
        native.set_session(controller.prepare, controller.guard, controller.finish)

    def options(self):
        """Return current plans/readback; values before first start are not board readback."""
        return self.native.options()

    def configure(self, profile, sample_rate, center_frequency=None):
        """Set next plans while stopped; apply hardware at next start and require Fs/readback agreement."""
        frequency = self.options().center_frequency if center_frequency is None else center_frequency
        configure_native(self.native,profile,sample_rate,frequency)
        self.controller.profile = profile or None

    def set_center_frequency(self, frequency):
        """Set the next stopped-state plan; metadata changes do not mean completed hardware tuning."""
        self.native.set_center_frequency(frequency)

    def error(self):
        return self.native.error()

    def produced(self):
        return self.native.produced()

    def received(self, channel):
        return self.native.received(channel)

    def queue_stats(self, channel):
        """Return native IIO backpressure statistics without device reads or inferred hardware continuity."""
        return self.native.queue_stats(channel)

    def tx_stats(self, channel):
        """Distinguish Sink acceptance, socket transfer and verified SSI consumption; USB submission is not consumption."""
        return self.native.tx_stats(channel)

    def stop_tx(self):
        """Stop continuous TX at a future common frame boundary; upstream supplies data until natural graph completion."""
        return self.native.request_tx_stop()

    def shutdown(self):
        """Explicitly cancel data/ownership to stop TX without restarting either direction."""
        self.native.shutdown()

    def ready(self):
        return self.native.ready()

    def stop_rx(self):
        """USB stops the board then drains full frames; follow with graph.wait/close, unlike forced graph.stop tail interruption."""
        if self.options().backend=='usb':self.controller.guard(0,'usb-stop')
        else:self.native.request_rx_stop()

    def close(self):
        """Cancel/destroy data before independent post; never report unconfirmed recovery as success."""
        self.native.shutdown()
        if hasattr(self.controller,'finish_errors'):self.controller.finish()
        if self.native.error():
            raise RuntimeError(self.native.error())

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class OwnerController:
    """Serialize native guards/tuning; background renews local permits only; IQ never calls Python.

    Never record passwords; monitors weakly reference Native; lifecycle threads call prepare/finish.
    """
    def __init__(self, native, host, username, known_hosts, runtime, lock_path,
                 firmware_lock, evidence, profile, password_provider, machine_id=None, pattern="normal"):
        self.native_ref = weakref.ref(native)
        self.host, self.username = host, username
        self.known_hosts, self.runtime = Path(known_hosts), Path(runtime)
        self.lock_path, self.firmware_lock = Path(lock_path), Path(firmware_lock)
        self.evidence = Path(evidence)
        self.profile, self.provider, self.machine_id = profile or None, password_provider, machine_id
        if pattern not in ("normal", "ramp16"): raise ValueError("INVALID_STREAM_PATTERN")
        self.pattern = pattern
        self.link = None
        self.connection = None
        self.monitor = None
        self.done = Event()
        self.command_lock = Lock()
        self.run_output = None
        self.recovery_confirmed = False
        self.guard_records = {}
        self.pause_renewal = False  # Test injection point; normal operation keeps checking native heartbeat.

    def _save(self, name, value):
        """Save this run's control summaries/receipts without serializing Connection or credential providers."""
        if self.run_output:
            (self.run_output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

    def prepare(self, mask):
        """Prepare once after all Source.start calls aggregate; preparation failures still require independent cleanup."""
        if self.link is not None:
            raise RuntimeError('OWNER_ALREADY_ACTIVE')
        native = self.native_ref()
        if native is None:
            raise RuntimeError('DEVICE_NO_LONGER_HELD')
        options = native.options()
        channels = [c for c in (1,2) if mask & (1 << (c-1))]
        if options.sample_rate*4*len(channels) > 64_000_000:
            raise RuntimeError('IIO_RX_BUDGET_EXCEEDED_BEFORE_CONTROL')
        backend = load_runtime(self.runtime,self.lock_path)
        self.run_output = self.evidence/('session-'+uuid.uuid4().hex)
        self.run_output.mkdir(mode=0o700,parents=True,exist_ok=False)
        self.recovery_confirmed = False
        self.guard_records = {}
        self.done = Event()
        self.connection = backend.Connection(self.host)
        self._save('request.json',dict(host=self.host,channels=channels,profile=self.profile,
            expected_rate_hz=options.sample_rate,requested_frequency_hz=options.center_frequency,
            backend='iio',RF_TX=False,started_at=datetime.now(timezone.utc).isoformat()))
        try:
            # Read target identity before configuration writes; same-profile restarts never reload the profile unnecessarily.
            state = backend.inspect(self.connection)
            identity = state['actual']['identity']
            firmware = json.loads(self.firmware_lock.read_text())
            if identity['boot_pins'] != firmware['boot_pins']:
                raise RuntimeError('FIRMWARE_BOOT_PINS_MISMATCH')
            if self.machine_id and identity['machine_id'] != self.machine_id:
                raise RuntimeError('EXPECTED_MACHINE_ID_MISMATCH')
            if state['actual']['kernel'] != firmware['kernel']:
                raise RuntimeError('FIRMWARE_KERNEL_MISMATCH')
            digest = state['actual']['metadata_sha256']
            if digest != firmware['metadata_sha256']:
                raise RuntimeError('FIRMWARE_METADATA_MISMATCH')
            if self.profile and state['info']['active'] != self.profile:
                state = backend.inspect(self.connection,self.profile)
                if state['actual']['identity'] != identity:
                    raise RuntimeError('IDENTITY_CHANGED_DURING_PROFILE_SELECTION')
            self.info = deepcopy(state['info'])
            params = {v['channel']:v for v in self.info['parameters']['rx']}
            if any(params[c]['sample_rate_hz'] != options.sample_rate for c in channels):
                raise RuntimeError('FLOWGRAPH_ACTUAL_RATE_MISMATCH')
            self._save('identity.json',state)
            self.link = backend.SessionLink(self.connection,self.info,channels,'live',self.pattern,backend.board_for(self.connection))
            self.link.start()
            # Write each LO group once; native readback/recovery covers shared ports.
            for group in self.info['radio']['groups']:
                if not set(group['channels']).intersection(channels):
                    continue
                if not group['editable']:
                    raise RuntimeError('RX_LO_GROUP_NOT_EDITABLE')
                if any(self.info['radio']['rx_center_frequency_hz'][str(c)] != int(options.center_frequency) for c in group['channels']):
                    reply = self.link.tune(int(options.center_frequency),group['channels'])
                    self.info['radio'] = reply['radio']
            self.link.info = self.info
            if not self.link.permitted():
                raise RuntimeError('OWNER_INITIAL_HEARTBEAT_MISSING')
            options.boot_id = identity['boot_id']
            options.configuration = self.info['active']
            options.rf_bandwidth = params[channels[0]]['rf_bandwidth_hz']
            options.center_frequency = self.info['radio']['rx_center_frequency_hz'][str(channels[0])]
            if any(self.info['radio']['rx_center_frequency_hz'][str(c)] != options.center_frequency for c in channels):
                raise RuntimeError('CURRENT_P1_REQUIRES_EQUAL_REQUESTED_RX_CENTERS')
            options.metadata_json = json.dumps(dict(self.info,stream_source=self.pattern),separators=(',',':'))
            native.renew_lease()
            self.monitor = Thread(target=self._monitor,name='h102-gr-owner-health',daemon=True)
            self.monitor.start()
            self._save('ready.json',dict(native=self.link.ready,active_info=self.info))
            return options
        except Exception:
            self.finish()
            raise

    def _monitor(self):
        """Forward verified native health only; disconnect revokes permission without reconnecting/restarting capture."""
        while not self.done.wait(.5):
            native = self.native_ref()
            if native is None:
                break
            if not self.link or not self.link.permitted():
                native.invalidate('OWNER_PERMISSION_EXPIRED')
                break
            if not self.pause_renewal:
                native.renew_lease()
            native = None

    def guard(self, channel, stage):
        """C++ calls begin/arm/end at buffer boundaries; serialize both readers' control requests."""
        with self.command_lock:
            if not self.link or not self.link.permitted():
                raise RuntimeError('GUARD_OWNER_PERMISSION_EXPIRED')
            result = self.link.guard(stage,int(channel))
            self.guard_records.setdefault(str(channel),{})[stage] = result
            self._save('guards.json',self.guard_records)

    def finish(self):
        """After data stops, close/check independent post and release created ownership even after early failure."""
        self.done.set()
        if self.monitor:
            self.monitor.join(timeout=5)
            if self.monitor.is_alive():
                raise RuntimeError('OWNER_HEALTH_THREAD_NOT_STOPPED')
            self.monitor = None
        link, self.link = self.link, None
        try:
            if link is None:
                return
            manager = link.close()
            records = (manager or {}).get('records',{})
            if not records.get('post.json'):
                backend = load_runtime(self.runtime,self.lock_path)
                records = backend.collect_session(link.board,link.directory,self.info['boot_id'])
            post = records.get('post.json',{})
            self.recovery_confirmed = bool(post.get('passed') and post.get('configuration_unchanged') and post.get('settings_restored'))
            self._save('control-end.json',dict(manager=manager,records=records,
                recovery_confirmed=self.recovery_confirmed,guards=self.guard_records))
            if any('arm' in row and not row.get('end',{}).get('passed') for row in self.guard_records.values()):
                raise RuntimeError('STREAM_GUARD_END_NOT_CONFIRMED')
            if not self.recovery_confirmed or not manager or manager.get('returncode'):
                raise RuntimeError('INDEPENDENT_OWNER_POST_NOT_CONFIRMED')
        finally:
            if self.connection:
                self.connection = None

# End of file: managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
