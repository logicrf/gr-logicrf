"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file iio_tx_managed.py
@brief Connect independent RF-off IIO TX ownership to native GNU Radio finite submission.
@details Reuse fixed SSH heartbeat/identity queries; Python handles no IQ and never calls the original RF TX actor.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.6.0
@date 2026-10-16
"""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event,Thread
import time
import uuid

from .managed import OwnerController,load_runtime


class IioTxController(OwnerController):
    """Serialize prepare/finish; monitor renews local permits only, without TX tuning or RF enable."""
    payload_names=('board_tx.py','board_radio.py','tx_cyclic.py')
    entrypoint='board_iio_tx.py'
    allow_tuning=False
    parallel_idle_warmup=False

    def _prepare_progress(self, stage, **fields):
        """Record actual preparation steps through an optional callback without networking or Qt operations."""
        row=dict(stage=stage, elapsed_seconds=time.monotonic()-self.prepare_started, **fields)
        self.prepare_stages.append(row)
        self._save('startup.json',dict(stages=self.prepare_stages))
        callback=getattr(self,'startup_notify',None)
        if callback is not None:callback(row)

    def _new_link(self, backend, info, channels, options):
        """Freeze allowlisted local source; idle warmup creates/sends no hardware start requests."""
        payload={n:base64.b64encode((self.runtime/'h102_iio_demo'/n).read_bytes()).decode()
                 for n in self.payload_names}
        payload[self.entrypoint]=base64.b64encode(Path(__file__).with_name(self.entrypoint).read_bytes()).decode()
        controller=self
        class DigitalLink(backend.SessionLink):
            """Read verified info/plans after start only; warm neither reads hardware nor grants data permission."""
            sources=payload
            entrypoint=controller.entrypoint
            start_timeout=105
            def expected(self):
                return controller.expected_request(options,channels)
        return DigitalLink(self.connection,info,channels,'iio-tx','normal',backend.board_for(self.connection)),payload

    def expected_request(self,options,channels):
        """Build frozen digital requests; derived sessions explicitly extend direction/RF plans."""
        return dict(info=self.info,channels=channels,samples=options.tx_samples,
            rate=options.sample_rate,frequency=int(options.center_frequency))

    def prepare(self,mask):
        """Verify identity before profile selection, and actual TX Fs/frequency/source silence before data buffers."""
        if self.link is not None:raise RuntimeError('OWNER_ALREADY_ACTIVE')
        native=self.native_ref()
        if native is None:raise RuntimeError('DEVICE_NO_LONGER_HELD')
        options=native.options();channels=[c for c in (1,2) if mask&(1<<(c-1))]
        if options.sample_rate*4*len(channels)>64_000_000:raise RuntimeError('IIO_TX_BUDGET_EXCEEDED_BEFORE_CONTROL')
        if options.tx_samples/options.sample_rate>300:raise RuntimeError('IIO_TX_FINITE_DURATION_EXCEEDED')
        if self.pattern!='normal':raise ValueError('IIO_TX_REQUIRES_NORMAL_DIGITAL_SUBMISSION')
        backend=load_runtime(self.runtime,self.lock_path)
        self.run_output=self.evidence/('session-'+uuid.uuid4().hex)
        self.run_output.mkdir(mode=0o700,parents=True,exist_ok=False)
        self.recovery_confirmed=False;self.guard_records={};self.done=Event()
        self.connection=backend.Connection(self.host)
        self._save('request.json',dict(host=self.host,channels=channels,profile=self.profile,
            samples=options.tx_samples,sample_rate=options.sample_rate,frequency=options.center_frequency,RF_TX=False))
        self.prepare_started=time.monotonic();self.prepare_stages=[]
        warmup=None
        try:
            self.check_cancelled()
            self._prepare_progress('identity')
            if self.parallel_idle_warmup and getattr(backend,'CONTROL_TRANSPORT',None)=='h102-api-v1':
                from .idle_warmup import IdleWarmup
                idle_link,payload=self._new_link(backend,{},channels,options)
                warmup=IdleWarmup(idle_link)
            state=backend.inspect(self.connection);identity=state['actual']['identity']
            self.check_cancelled()
            firmware=json.loads(self.firmware_lock.read_text())
            if identity['boot_pins']!=firmware['boot_pins']:raise RuntimeError('FIRMWARE_BOOT_PINS_MISMATCH')
            if self.machine_id and identity['machine_id']!=self.machine_id:raise RuntimeError('EXPECTED_MACHINE_ID_MISMATCH')
            if state['actual']['kernel']!=firmware['kernel']:raise RuntimeError('FIRMWARE_KERNEL_MISMATCH')
            digest=state['actual']['metadata_sha256']
            if digest!=firmware['metadata_sha256']:raise RuntimeError('FIRMWARE_METADATA_MISMATCH')
            if self.profile and state['info']['active']!=self.profile:
                self._prepare_progress('profile',current_profile=state['info']['active'],target_profile=self.profile)
                keywords={'current_state':state} if getattr(backend,'CONTROL_TRANSPORT',None)=='h102-api-v1' else {}
                state=backend.inspect(self.connection,self.profile,**keywords)
                if state['actual']['identity']!=identity:raise RuntimeError('IDENTITY_CHANGED_DURING_PROFILE_SELECTION')
            self.check_cancelled()
            self.info=deepcopy(state['info']);kind='tx' if options.direction=='tx' else 'rx'
            params={v['channel']:v for v in self.info['parameters'][kind]}
            self._save('identity.json',state)
            if any(params[c]['sample_rate_hz']!=options.sample_rate for c in channels):raise RuntimeError('FLOWGRAPH_ACTUAL_TX_RATE_MISMATCH')
            if not self.allow_tuning and any(self.info['radio'][kind+'_center_frequency_hz'][str(c)]!=options.center_frequency for c in channels):
                raise RuntimeError('IIO_TX_REQUIRES_CURRENT_ACTUAL_TX_FREQUENCY')
            self._prepare_progress('owner')
            request=self.expected_request(options,channels)
            if warmup:
                self.link=warmup.claim(self.info,request)
            else:
                self.link,payload=self._new_link(backend,self.info,channels,options)
                self.link.expected=lambda:request
            self.configure_link(self.link)
            self.link.start()
            self.check_cancelled()
            if self.link.ready.get('radio'):self.info['radio']=self.link.ready['radio']
            for key in ('rx_gain_readback','tx_gain_readback'):
                if key in self.link.ready:self.info[key]=self.link.ready[key]
            if not self.link.permitted():raise RuntimeError('OWNER_INITIAL_HEARTBEAT_MISSING')
            options.boot_id=identity['boot_id'];options.configuration=self.info['active']
            options.rf_bandwidth=params[channels[0]]['rf_bandwidth_hz']
            options.metadata_json=json.dumps(dict(self.info,RF_TX=request.get('rf_enabled',False),tx_evidence='network_submission'),separators=(',',':'))
            self._save('source-payload-sha256.json',{n:hashlib.sha256(base64.b64decode(v)).hexdigest() for n,v in payload.items()})
            self._save('ready.json',dict(native=self.link.ready,active_info=self.info))
            self._prepare_progress('buffers',native_timings=dict(self.link.timings))
            native.renew_lease();self.monitor=Thread(target=self._monitor,name='h102-gr-iio-tx-owner',daemon=True)
            self.monitor.start();return options
        except Exception:
            self.finish();raise
        finally:
            if warmup is not None:warmup.close()

    def check_cancelled(self):
        """Default controller has no cancellation source; interactive subclasses check cancellation at preparation boundaries."""

    def configure_link(self, link):
        """Preserve default lifecycle; subclasses may bind explicit user cancellation events."""

    def guard(self,channel,stage):
        """Check fresh native health at start/end boundaries only, without claiming SSI continuity guards."""
        with self.command_lock:
            if stage not in ('iio-tx-begin','iio-tx-end') or channel not in (1,2):raise ValueError('IIO_TX_GUARD_STAGE')
            if not self.link or not self.link.permitted():raise RuntimeError('IIO_TX_OWNER_PERMISSION_EXPIRED')
            result=dict(permitted=True,stage=stage,channel=channel,RF_TX=False,ssi_consumption_verified=False)
            self.guard_records.setdefault(str(channel),{})[stage]=result
            self._save('guards.json',self.guard_records)

# End of file: iio_tx_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
