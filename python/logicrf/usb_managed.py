"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_managed.py
@brief Coordinate multirate board RX ownership and a separate local FT601 process with verified C++ configuration.
@details Python handles low-rate control only; both endpoints retain native main/post; ACK follows verified STOP.
         One Device shares dual USB frames; credentials stay in memory; IQ never passes through this module.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.3
@date 2026-10-16
"""
from datetime import datetime,timezone
import base64
import hashlib
import json
import os
from pathlib import Path
from queue import Queue,Empty
import re
import shlex
import socket
from threading import Event,Lock,RLock,Thread
import time
import uuid
import weakref
from .managed import load_runtime


def verify_loaded_bit(identity, pins):
    """Verify pinned UART loading provenance for one boot candidate; released firmware locks keep existing behavior.

    active_bit comes only from an independently installed candidate lock, never arbitrary device-returned policy.
    File existence/capability cannot replace same boot, exact path and original UART hash.
    This binds loading provenance, not physical FPGA configuration-memory readback.
    """
    policy = pins.get('active_bit')
    if policy is None:
        return
    fields = {'bit_path', 'bit_sha256', 'uart_raw_sha256', 'boot_id',
              'compiled_commit', 'routed_sha256'}
    if not isinstance(policy, dict) or set(policy) != fields:
        raise RuntimeError('USB_CANDIDATE_BIT_POLICY')
    for key, length in (('bit_sha256',64), ('uart_raw_sha256',64),
                        ('compiled_commit',40), ('routed_sha256',64)):
        if not isinstance(policy[key], str) or not re.fullmatch('[a-f0-9]{%d}' % length, policy[key]):
            raise RuntimeError('USB_CANDIDATE_BIT_POLICY_DIGEST')
    path = policy['bit_path']
    if (not isinstance(path, str) or Path(path).parent != Path('/boot') or
            str(Path(path)) != path or not path.endswith('.bit') or
            pins['boot_pins'].get(path) != policy['bit_sha256']):
        raise RuntimeError('USB_CANDIDATE_BIT_POLICY_PIN')
    record = identity.get('loaded_bit')
    expected = dict(schema=1, boot_id=identity['boot_id'], machine_id=identity['machine_id'],
                    MAC=identity['MAC'], bit_path=path, bit_sha256=policy['bit_sha256'],
                    uart_raw_sha256=policy['uart_raw_sha256'])
    if (identity['boot_id'] != policy['boot_id'] or record != expected or
            type(record.get('schema')) is not int):
        raise RuntimeError('USB_CANDIDATE_BIT_LOADING_PROOF')


class UsbController:
    """Dual-endpoint Device controller; serialize commands and check board/host health every 0.4 s before permit renewal.

    Never install root components or accept arbitrary sudo commands; use frozen host guards/board modules only.
    Base sessions are digital; RF plans require explicit variants. Change profiles/carriers only while stopped.
    """
    def __init__(self,native,host,username,known_hosts,runtime,lock_path,firmware_lock,evidence,
                 profile,password_provider,machine_id,project,host_guard,host_password_provider,seconds):
        self.native_ref=weakref.ref(native);self.host=host;self.username=username
        self.known_hosts=Path(known_hosts);self.runtime=Path(runtime);self.lock_path=Path(lock_path)
        self.firmware_lock=Path(firmware_lock);self.evidence=Path(evidence);self.profile=profile
        self.provider=password_provider;self.machine_id=machine_id;self.project=Path(project)
        self.host_guard=Path(host_guard);self.host_provider=host_password_provider;self.seconds=seconds
        if type(seconds) is not int or seconds not in (0,10,300):raise ValueError('USB_DURATION_10_OR_300')
        self.command_lock=Lock();self.stop_lock=Lock();self.host_lock=RLock();self.done=Event();self.replies=Queue(maxsize=8)
        self.client=None;self.monitor=None;self.board_input=None;self.board_channel=None
        self.sequence=0;self.ready=None;self.host_session=None;self.host_password=None;self.finish_errors=[]
        self.board_stopped=False;self.started=False;self.recovery_confirmed=False;self.guard_records={}
        self.host_launch=None;self.host_status=None
        self.pause_renewal=False;self.run_output=None;self.failure=None;self.finished=False;self.transport_previous={};self.transport_observations=0

    def _save(self,name,value):
        """Save bounded control facts without serializing connections, passwords or session tokens."""
        if self.run_output:(self.run_output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

    def native_devices(self):
        """Native engines actually owned by this controller; shared duplex variants override this set."""
        native=self.native_ref()
        return (native,) if native is not None else ()

    def renew_local(self):
        """Renew all local permits only after native health verification."""
        for native in self.native_devices():native.renew_lease()

    def invalidate_local(self,reason):
        """Revoke every owned direction together on any control failure."""
        for native in self.native_devices():native.invalidate(reason)

    def masks(self,mask,transmit):
        """Default unidirectional masks; shared variants override explicitly within the ten native modes."""
        return (0,mask) if transmit else (mask,0)

    def validate_extra(self,state):
        """Unidirectional default adds no peer constraints; duplex variants verify actual peer parameters."""
        return None

    def host_request(self,options,mask,transmit):
        """Reject caller program paths; submit only exact data shapes supported by this installation."""
        request=dict(socket=options.usb_socket,seconds=self.seconds,rate=int(options.sample_rate))
        if transmit:request.update(direction='tx',mask=mask,samples=options.tx_samples)
        return request

    def board_preamble(self):
        """Import the pinned released digital runtime; derived variants extend new private directories only."""
        return "sys.path.insert(0,'/usr/local/lib/h102/sdk1/scripts')"

    def _host(self,action,value=None,timeout=15):
        """UNIX interface calls the installed fixed guard only, without passwords or caller-supplied program paths."""
        from h102_control.protocol import encode_frame, receive_frame
        with self.host_lock:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                peer.settimeout(timeout)
                try:peer.connect('/run/logicrf-h102-usb/control.sock')
                except OSError as error:raise RuntimeError('Install the H102 USB host service first; ordinary operation does not require a sudo password') from error
                peer.sendall(encode_frame(dict(action=action,session=self.host_session,value=value)))
                with peer.makefile('rb') as reader:result=receive_frame(reader)
        if result.get('ok') is not True:raise RuntimeError('USB_HOST_'+action.upper()+':'+str(result.get('error'))[-1800:])
        return result['result']

    def _receive(self,stream):
        """Sole SSH reader with at most eight pending replies; read control JSON only, never IQ."""
        try:
            while True:
                line=stream.readline(65537)
                if not line:break
                if len(line)>65536:raise RuntimeError('USB_BOARD_REPLY_TOO_LARGE')
                self.replies.put(json.loads(line),timeout=2)
        except Exception as e:self.failure=repr(e)
        finally:
            try:self.replies.put(dict(error='USB_BOARD_BRIDGE_EOF'),timeout=2)
            except Exception:pass

    def _command(self,op,**values):
        """Serialize strictly increasing native commands; missing replies never imply success."""
        with self.command_lock:
            self.sequence+=1
            r=dict(op=op,token=self.ready['token'],seq=self.sequence,**values)
            self.board_input.write(json.dumps(r)+'\n');self.board_input.flush()
            try:reply=self.replies.get(timeout=3)
            except Empty:raise TimeoutError('USB_BOARD_REPLY_TIMEOUT')
            if reply.get('seq')!=self.sequence or reply.get('ok') is not True:
                raise RuntimeError('USB_BOARD_REPLY_SEQUENCE_OR_FAILURE:'+str(reply))
            self.last_reply=reply
            native=reply.get('native')
            if native:
                path=self.run_output/'native-snapshots.jsonl'
                if path.exists() and path.stat().st_size>4*1024*1024:path.replace(path.with_suffix('.previous.jsonl'))
                with path.open('a') as stream:
                    stream.write(json.dumps(dict(op=op,seq=self.sequence,native=native),separators=(',',':'))+'\n')
                transport=native.get('transport')
                if transport and self.started:
                    w=transport['words']
                    for channel in (1,2):
                        if not self.expected['rx_mask']&(1<<(channel-1)):continue
                        base=(channel-1)*10;count=w[base]|w[base+1]<<32;drops=w[base+2]|w[base+3]<<32
                        previous=self.transport_previous.get(channel,0)
                        if drops or count<previous:raise RuntimeError('USB_NATIVE_DROPS_OR_COUNT_REGRESSION')
                        self.transport_previous[channel]=count
                    self.transport_observations+=1
            return reply

    def _inspect_profile(self,backend,pins):
        """Select profiles from a complete snapshot/check receipts without duplicate status or cached hardware facts.

        Check firmware/machine before changes; profile.select carries verified boot and returns a new complete snapshot.
        Still verify identity/SDK/profile; never retry uncertain changes; callers use normal failure recovery.
        """
        state=backend.inspect(self.connection);identity=state['actual']['identity']
        if (identity['boot_pins']!=pins['boot_pins'] or state['actual']['kernel']!=pins['kernel'] or
                self.machine_id and identity['machine_id']!=self.machine_id):
            raise RuntimeError('USB_BOARD_IDENTITY')
        if state['actual']['metadata_sha256']!=pins['metadata_sha256']:
            raise RuntimeError('USB_BOARD_METADATA_SHA')
        verify_loaded_bit(identity,pins)
        if self.profile and state['info']['active']!=self.profile:
            from h102_control.client import Client
            state=Client(self.connection).request('profile.select',
                dict(profile=self.profile,boot_id=identity['boot_id']),timeout=240)
            if state['info']['api']!='68.21.6':raise RuntimeError('CONTROL_API_REQUIRES_H102_SDK1_68_21_6')
            if state['info']['active']!=self.profile:raise RuntimeError('USB_PROFILE_READBACK')
        if state['actual']['identity']!=identity:raise RuntimeError('USB_PROFILE_CHANGED_IDENTITY')
        if (state['actual']['kernel']!=pins['kernel'] or
                state['actual']['metadata_sha256']!=pins['metadata_sha256']):
            raise RuntimeError('USB_PROFILE_CHANGED_FIRMWARE')
        return state

    def prepare(self,mask):
        """Verify board/released boot inputs before ownership/FT preparation; construction remains IO-free."""
        if self.client is not None:raise RuntimeError('USB_OWNER_ALREADY_ACTIVE')
        self.host_launch=None;self.host_status=None;self.host_session=None;self.ready=None
        self.guard_records={};self.monitor=None
        native=self.native_ref();options=native.options()
        transmit=options.direction=='tx';rx_mask,tx_mask=self.masks(mask,transmit)
        self.run_output=self.evidence/('session-'+uuid.uuid4().hex);self.run_output.mkdir(mode=0o700,parents=True,exist_ok=False)
        self.done=Event();self.failure=None;self.recovery_confirmed=False;self.board_stopped=False;self.started=False
        self.finished=False;self.finish_errors=[];self.restoration_confirmed=False;self.sequence=0;self.replies=Queue(maxsize=8);self.transport_previous={};self.transport_observations=0
        backend=load_runtime(self.runtime,self.lock_path)
        self.connection=backend.Connection(self.host)
        try:
            state=self._inspect_profile(backend,json.loads(self.firmware_lock.read_text()))
            identity=state['actual']['identity']
            channels=[c for c in (1,2) if mask&(1<<(c-1))]
            params={p['channel']:p for p in state['info']['parameters']['tx' if transmit else 'rx']}
            if any(params[c]['sample_rate_hz']!=options.sample_rate for c in channels):raise RuntimeError('USB_ACTUAL_RATE')
            if not getattr(self,'radio_control',False) and transmit and any(state['info']['radio']['tx_center_frequency_hz'][str(c)]!=int(options.center_frequency) for c in channels):
                raise RuntimeError('USB_TX_REQUIRES_VERIFIED_CURRENT_CENTER')
            if not getattr(self,'radio_control',False) and not transmit and any(state['info']['radio']['rx_center_frequency_hz'][str(c)]!=int(options.center_frequency) for c in channels):
                backend.tune_rx_idle(self.connection,state,channels,int(options.center_frequency),self.run_output/'tune')
                state=backend.inspect(self.connection)
            self.validate_extra(state)
            self._save('identity.json',state);self.info=state['info'];self.identity=identity
            from h102_control.client import Client
            from h102_control.channel import SessionChannel
            from h102_control.source_identity import installed_sources
            from .api_pipe import Pipe
            names=('usb_runtime_candidate.py','gr_usb_radio.py','board_tx.py','board_radio.py','tx_cyclic.py')
            sources={}
            for name in names:
                path=(Path(__file__).with_name('board_usb_radio.py' if name=='gr_usb_radio.py' else name)
                      if name in ('usb_runtime_candidate.py','gr_usb_radio.py') else self.runtime/'h102_iio_demo'/name)
                sources[name]=hashlib.sha256(path.read_bytes()).hexdigest()
            if getattr(self,'reuse_board',False):
                names=names+('usb_rx_reuse_candidate.py','gr_usb_reuse.py')
                for name in names[-2:]:sources[name]=hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            sources=installed_sources(sources)
            request=dict(profile=self.info['active'],boot_id=identity['boot_id'],rx_mask=rx_mask,tx_mask=tx_mask,
                seconds=self.seconds,transmit=transmit,arbitrary=bool(getattr(self,'radio_control',False) or options.tx_validation=='none' and transmit),
                radio_plan=self.radio_plan() if getattr(self,'radio_control',False) else None,sources=sources)
            if getattr(self,'reuse_board',False):request['reusable']=True
            peer,reader,receipt=Client(self.connection).open_stream('usb.open',request)
            self.board_channel=self.client=SessionChannel(peer,reader)
            if receipt['sources']!=sources:raise RuntimeError('USB_API_NATIVE_SOURCE_RECEIPT')
            self.api_session_id=receipt['session_id'];self.board_directory=receipt['directory']
            if request['arbitrary']:self.code_root=receipt['code_root']
            self.board_input=Pipe(self.board_channel,'stdin');out=Pipe(self.board_channel,'stdout')
            self.board_stderr=Pipe(self.board_channel,'stderr')
            self.reader=Thread(target=self._receive,args=(out,),daemon=True,name='h102-gr-usb-board');self.reader.start()
            launch=self.replies.get(timeout=15)
            if not launch.get('launched'):raise RuntimeError('USB_BOARD_LAUNCH:'+str(launch))
            self.ready=self.replies.get(timeout=65)
            if not self.ready.get('ready'):raise RuntimeError('USB_BOARD_READY:'+str(self.ready))
            if getattr(self,'radio_control',False):
                if not self.ready.get('radio'):raise RuntimeError('USB_RADIO_READBACK_MISSING')
                self.info['radio']=self.ready['radio']
                if self.ready.get('rf_requested') is not getattr(self,'rf_enabled',False):raise RuntimeError('USB_RADIO_RF_READBACK_MISMATCH')
                self.info['rx_gain_readback']=self.ready['rx_gain_readback'];self.info['tx_gain_readback']=self.ready['tx_gain_readback']
                for ch,gain in getattr(self,'rx_gain_db',{}).items():
                    if abs(float(self.ready['rx_gain_readback'][str(ch)].split()[0])-gain)>.001:raise RuntimeError('USB_RADIO_RX_GAIN_READBACK')
                for ch in getattr(self,'tx_channels',()):
                    if abs(float(self.ready['tx_gain_readback'][str(ch)].split()[0])+self.attenuation_db)>.001:raise RuntimeError('USB_RADIO_TX_GAIN_READBACK')
                for ch in channels:
                    actual_hz=self.info['radio'][('tx' if transmit else 'rx')+'_center_frequency_hz'][str(ch)]
                    if actual_hz!=int(options.center_frequency):raise RuntimeError('USB_RADIO_ACTUAL_FREQUENCY')
            prepared=self.ready['prepared'];self.expected=prepared
            if prepared['rx_mask']!=rx_mask or prepared['tx_mask']!=tx_mask or prepared['device_id']!=str(int(identity['MAC'].replace(':',''),16)):
                raise RuntimeError('USB_NATIVE_BOARD_BINDING')
            options.boot_id=identity['boot_id'];options.configuration=self.info['active']
            options.rf_bandwidth=params[channels[0]]['rf_bandwidth_hz']
            options.metadata_json=json.dumps(dict(self.info,stream_source='native_usb_tx_digital' if transmit else 'native_usb_rx',RF_TX=getattr(self,'rf_enabled',False)),separators=(',',':'))
            config=bytes.fromhex(self.ready['configuration_hex'])
            if len(config)!=(256 if transmit else 112) or hashlib.sha256(config).hexdigest()!=prepared['tx_sha256' if transmit else 'rx_sha256']:raise RuntimeError('USB_NATIVE_CONFIG_SHA')
            configuration=self.run_output/'configuration.bin';configuration.write_bytes(config)
            if transmit:options.usb_tx_templates=str(configuration);options.usb_tx_seed=prepared['seed']
            else:options.usb_configuration=str(configuration)
            options.usb_stream_id=int(prepared['tx_stream_id' if transmit else 'rx_stream_id']);options.usb_time_epoch=prepared['configuration_id']
            socket_root=self.project/'envs/gr-usb';socket_root.mkdir(mode=0o700,parents=True,exist_ok=True)
            if socket_root.stat().st_mode&0o077:raise RuntimeError('USB_SOCKET_DIRECTORY_PERMISSIONS')
            options.usb_socket=str(socket_root/(uuid.uuid4().hex+'.sock'))
            self.host_session='h102-gr-usb-'+uuid.uuid4().hex
            self.renew_local()
            self.monitor=Thread(target=self._monitor,name='h102-gr-usb-owner',daemon=True);self.monitor.start()
            self.host_launch=self._host('launch',self.host_request(options,mask,transmit))
            self._save('ready.json',dict(prepared=prepared,board_directory=self.board_directory,host_session=self.host_session,
                configuration_sha256=hashlib.sha256(config).hexdigest(),RF_TX=getattr(self,'rf_enabled',False),started_at=datetime.now(timezone.utc).isoformat()))
            return options
        except Exception:
            # Retain bounded stderr even for pre-start errors, otherwise capacity/source errors collapse into bridge EOF.
            if self.board_channel and self.board_channel.exit_status_ready():
                chunks=[];remaining=65536
                while remaining and self.board_channel.recv_stderr_ready():
                    raw=self.board_channel.recv_stderr(min(4096,remaining));chunks.append(raw);remaining-=len(raw)
                self._save('board-launch-error.json',dict(exit_status=self.board_channel.recv_exit_status(),
                    stderr=b''.join(chunks).decode(errors='replace'),truncated=remaining==0))
            self.finish();raise

    def _monitor(self):
        """Renew after native commands/host health; natural receive expiry uses the same STOP/ACK."""
        while not self.done.wait(.4):
            native=self.native_ref()
            if native is None:return
            try:
                self._command('renew')
                if self.host_launch is not None:
                    status=self._host('status');state=status['state.json'] or {}
                    self.host_status=status
                    if state.get('error'):raise RuntimeError('USB_HOST_DATA_ERROR:'+status['stdout'][-1000:])
                    if state.get('stop_requested') and not self.board_stopped:self._stop_pair()
                    if state.get('running') is False and not status['main.json']:raise RuntimeError('USB_HOST_MAIN_NOT_PASS')
                if not self.pause_renewal:self.renew_local()
            except Exception as e:
                self.failure=repr(e);self.invalidate_local('USB_OWNER:'+str(e));return

    def guard(self,channel,stage):
        """Send actual READY/START after C++ connects the helper; stopping requires both endpoints' native proofs."""
        if stage=='usb-stop':self._stop_pair();return
        if stage!='usb-start':raise ValueError('USB_GUARD_STAGE')
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            status=self._host('status');self.host_status=status
            if (status['state.json'] or {}).get('ready'):break
            if self.failure:raise RuntimeError(self.failure)
            time.sleep(.03)
        else:raise TimeoutError('USB_REAL_HELPER_READY_TIMEOUT')
        self._command('receiver_ready',stream_id=self.expected['rx_stream_id'],configuration_sha256=self.expected['rx_sha256'])
        start=self._command('start');self.started=True;self.guard_records['start']=start;self._save('guards.json',self.guard_records)

    def _stop_pair(self):
        """Check board STOP, request receiver STOP_REQUEST, then ACK without restarting either direction."""
        with self.stop_lock:
            if self.board_stopped:return
            proof=self._command('stop');self.board_stopped=True;self.guard_records['stop']=proof
            if self.host_launch is not None:
                status=self._host('status');state=status['state.json'] or {}
                if state.get('running'):
                    if not state.get('stop_requested'):self._host('send',dict(op='stop'))
                    deadline=time.monotonic()+2
                    while not (self._host('status')['state.json'] or {}).get('stop_requested'):
                        if time.monotonic()>deadline:raise TimeoutError('USB_HOST_STOP_REQUEST_MISSING')
                        time.sleep(.02)
                    self._host('send',dict(op='ack',proof=proof))
            self._save('guards.json',self.guard_records)

    def finish(self):
        """Close board ownership only after actual USB close/post; verify independent board outer-post recovery."""
        if self.finished:
            if self.finish_errors:raise RuntimeError('USB_RECOVERY:'+str(self.finish_errors))
            return
        self.finished=True
        errors=[]
        try:
            if self.ready and self.ready.get('ready'):
                try:self._stop_pair()
                except Exception as e:errors.append('stop:'+repr(e))
            self.done.set()
            if self.monitor and self.monitor is not __import__('threading').current_thread():self.monitor.join(timeout=6)
            host_restored=bool(getattr(self,'retained_host_post_verified',False))
            if self.host_launch is not None:
                try:
                    deadline=time.monotonic()+12
                    while time.monotonic()<deadline:
                        status=self._host('status')
                        if status['main.json']:break
                        state=status['state.json'] or {}
                        if state.get('running') is False:
                            # Preserve main failures; successful independent post cannot turn the case into PASS.
                            if status['post.json']:
                                host_restored=status['post.json'].get('passed') is True
                                self._save('host-final.json',status)
                                raise RuntimeError('USB_HOST_FINAL_MAIN_FAILED:'+status['stdout'][-1000:])
                        time.sleep(.05)
                    else:raise TimeoutError('USB_HOST_MAIN_FINISH_TIMEOUT')
                    final=self._host('finish',timeout=20);self._save('host-final.json',final)
                    host_restored=final['post.json'].get('passed') is True
                    if not host_restored:raise RuntimeError('USB_HOST_POST_NOT_PASS')
                    if (getattr(self,'reuse_board',False) and
                            self.host_launch.get('capabilities',{}).get('epoch_archive_v1')):
                        from .epoch_evidence import retire_host
                        retire_host(self,getattr(self,'epoch_count',0))
                except Exception as e:errors.append('host:'+repr(e))
            if self.ready and self.ready.get('ready'):
                try:self._command('close')
                except Exception as e:errors.append('bridge-close:'+repr(e))
                self.reader.join(timeout=5)
                if self.reader.is_alive():raise RuntimeError('USB_BOARD_READER_NOT_JOINED')
                if self.board_channel.exit_status_ready():
                    self._save('board-bridge-stderr.json',self.board_stderr.read().decode(errors='replace'))
                deadline=time.monotonic()+50
                while True:
                    from h102_control.client import Client
                    records=Client(self.connection).request('usb.records',dict(session_id=self.api_session_id))
                    if records['outer-post.json']:break
                    if time.monotonic()>deadline:raise TimeoutError('USB_BOARD_OUTER_POST_DEADLINE')
                    time.sleep(.1)
                self._save('board-final.json',records)
                self.restoration_confirmed=(host_restored and records['outer-post.json'].get('status')=='OUTER_POST_PASS' and
                    (records['post.json'] or {}).get('status')=='POST_PASS')
                if getattr(self,'radio_control',False):
                    self.restoration_confirmed=self.restoration_confirmed and records['outer-post.json'].get('rf_restore',{}).get('passed') is True
                self.recovery_confirmed=(self.restoration_confirmed and (records['summary.json'] or {}).get('status')=='PASS_DIGITAL_RX_MAIN_POST')
                if not self.recovery_confirmed:raise RuntimeError('USB_BOARD_NATIVE_MAIN_POST_NOT_PASS')
                if getattr(self,'code_root',None):
                    from .runtime_archive import retire
                    retire(self)
        except Exception as e:errors.append(repr(e))
        finally:
            self.done.set()
            for stream in (getattr(self,'board_input',None),getattr(self,'board_stderr',None)):
                if stream:
                    try:stream.close()
                    except Exception:pass
            if self.client:self.client.close();self.client=None
            self.connection=None;self.host_password=None;self.ready=None
            self.finish_errors=list(errors)
            self._save('control-end.json',dict(recovery_confirmed=self.recovery_confirmed,restoration_confirmed=getattr(self,'restoration_confirmed',False),errors=errors,RF_TX=False,transport_observations=self.transport_observations,transport_counts=self.transport_previous))
        if errors:raise RuntimeError('USB_RECOVERY:'+str(errors))

# End of file: usb_managed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
