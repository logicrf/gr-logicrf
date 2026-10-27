"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_candidate.py
@brief Check generated continuous-SDK state machines, short permits and original checker restoration.
@details External SDK inputs must match every SHA; explicit register models never qualify boards/FT601.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from gnuradio.logicrf.usb_runtime_candidate import build,substitute


class UsbCandidateTests(unittest.TestCase):
    """Extend continuous/arbitrary IQ only without weakening two-second permits or original restoration."""
    def test_radio_candidate_compiles_exact_source_closure(self):
        """RF insertion points must match the full pinned SDK and include restoration/immediate shutdown."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT']);source=project/'envs/h102-sdk-runtime-input'/json.loads((Path(__file__).resolve().parents[1]/'docs/h102-usb-sdk.lock.json').read_text())['input_directory']
        if not source.is_dir():self.skipTest('external pinned H102 SDK source inputs not installed')
        repo=Path(__file__).resolve().parents[1]
        lock=json.loads((repo/'docs/h102-usb-sdk.lock.json').read_text())
        runtime=project/'envs/h102-control-runtime/f782657/h102_iio_demo'
        extras={name:(runtime/name).read_text() for name in ('board_tx.py','board_radio.py','tx_cyclic.py')}
        extras['gr_usb_radio.py']=(repo/'python/logicrf/board_usb_radio.py').read_text()
        plan=dict(configuration='4msps-2mhz-common',rx_channels=[1],tx_channels=[1],rx_frequency=1000000000,
            tx_frequency=1000000000,rf_enabled=True,attenuation_db=40,rx_gain_db={'1':20})
        with tempfile.TemporaryDirectory(prefix='usb-radio-qa-',dir=project/'tmp') as root:
            candidate=Path(root)/'code';report=build(candidate,lock,source,offline=True,radio_plan=plan,extra_sources=extras)
            self.assertTrue(report['RF_TX_requested']);self.assertEqual(len(report['candidate']),113)
            for path in candidate.glob('*.py'):compile(path.read_text(),path.name,'exec')
            actor=(candidate/'h102_runtime_usb_rx.py').read_text()
            self.assertLess(actor.index('gr_usb_radio.fast_stop(directory,request)'),actor.index('adapter=PreparationBoard'))
            self.assertIn('rf_restore=rf_restored',actor)
            self.assertIn("if prepared.rx_mask:",actor)

    def test_changed_sdk_anchor_fails_closed(self):
        with self.assertRaises(ValueError):substitute('unknown','expected','new')

    def test_legacy_and_paired_sdk_have_identical_executed_guards(self):
        """Both per-SHA SDK inputs produce equal checked-function ASTs after actual generation/model coverage."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT']);repo=Path(__file__).resolve().parents[1]
        results=[]
        for version,lock_name in [('1.2.0','h102-usb-sdk-1.2.0.lock.json'),
                                  (json.loads((repo/'docs/h102-usb-sdk.lock.json').read_text())['input_directory'],'h102-usb-sdk.lock.json')]:
            source=project/'envs/h102-sdk-runtime-input'/version
            if not source.is_dir():self.skipTest('external pinned '+version+' SDK not installed')
            lock=json.loads((repo/'docs'/lock_name).read_text())
            with tempfile.TemporaryDirectory(prefix='usb-sdk-compat-',dir=project/'tmp') as root:
                candidate=Path(root)/'code';build(candidate,lock,source,offline=True)
                profile=ast.parse((candidate/'h102_runtime_profile.py').read_text())
                cls=next(n for n in profile.body if isinstance(n,ast.ClassDef) and n.name=='RuntimeProfile')
                check=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='check')
                prepare=ast.parse((candidate/'h102_runtime_usb_prepare.py').read_text())
                verify=next(n for n in prepare.body if isinstance(n,ast.FunctionDef) and n.name=='verify_request')
                results.append((ast.dump(check),ast.dump(verify)))
        self.assertEqual(results[0],results[1])

    def test_generated_cold_capabilities_and_configuration_guards(self):
        """Execute generated code with modeled hardware; cap3 never exempts identity/parameter/source-idle checks."""
        project=Path(os.environ['LOGICRF_PROJECT_ROOT'])
        source=project/'envs/h102-sdk-runtime-input'/json.loads((Path(__file__).resolve().parents[1]/'docs/h102-usb-sdk.lock.json').read_text())['input_directory']
        if not source.is_dir():self.skipTest('external pinned H102 SDK source inputs not installed')
        lock=json.loads((Path(__file__).resolve().parents[1]/'docs/h102-usb-sdk.lock.json').read_text())
        with tempfile.TemporaryDirectory(prefix='usb-capability-',dir=project/'tmp') as root:
            candidate=Path(root)/'code';build(candidate,lock,source,offline=True)
            script=r'''
import hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,sys.argv[1])
import h102_runtime_profile as runtime
import h102_runtime_usb_prepare as prepare
state=Path(sys.argv[1]).parent/'state.json';state.write_bytes(b'{"fixture":true}\n')
identity=dict(MAC='02:00:00:00:00:01',boot_id='model-boot')
rx=dict(channel=1,sample_rate_hz=8000000,rf_bandwidth_hz=4000000)
tx=dict(channel=1,sample_rate_hz=8000000,rf_bandwidth_hz=4000000)
class Board:
    cap=1
    idle=True
    def read(self,offset):return self.cap if offset==0x424 else 0
    def validate(self):pass
    def sources_idle(self):return self.idle
board=Board();hw=SimpleNamespace(expected=identity.copy(),phy=object(),verify=Mock())
runtime._ssi_normal=Mock();runtime.profiles.private=Mock()
runtime.rx.read_applied=Mock(return_value=rx);runtime.tx.read_applied=Mock(return_value=tx)
prepare.profiles.identity=Mock(return_value=identity.copy());prepare.profiles.STATE=state
package=dict(id='model-8m',parameters=dict(rx=[rx]),blobs={})
profile=runtime.RuntimeProfile(hw,board,state,state.read_bytes(),package,rx,tx)
request=dict(identity=identity.copy(),state_sha256=hashlib.sha256(state.read_bytes()).hexdigest(),
             expected_profile=[8000000,4000000],rx_applied=rx.copy(),tx_applied=tx.copy())
def rejects(call,message):
    try:call()
    except RuntimeError as error:
        assert message in str(error),(message,str(error))
    else:raise AssertionError('guard accepted invalid model: '+message)
for cap in (1,3):
    board.cap=cap;profile.check();prepare.verify_request(hw,board,request)
for cap in (0,2,7):
    board.cap=cap
    rejects(profile.check,'RUNTIME_BIT_OR_IDENTITY_CHANGED')
    rejects(lambda:prepare.verify_request(hw,board,request),'PREPARATION_REQUEST_OR_ACTUAL_CHANGED')
board.cap=3;board.idle=False
rejects(lambda:prepare.verify_request(hw,board,request),'PREPARATION_SOURCE_ACTIVE');board.idle=True
runtime.rx.read_applied.return_value=dict(rx,rf_bandwidth_hz=2000000)
rejects(profile.check,'RUNTIME_ACTUAL_CONFIGURATION_CHANGED')
rejects(lambda:prepare.verify_request(hw,board,request),'PREPARATION_REQUEST_OR_ACTUAL_CHANGED')
runtime.rx.read_applied.return_value=rx
hw.expected=dict(identity,boot_id='foreign-boot')
rejects(profile.check,'RUNTIME_BIT_OR_IDENTITY_CHANGED');hw.expected=identity.copy()
state.write_bytes(b'changed')
rejects(profile.check,'RUNTIME_ACTIVE_PROFILE_CHANGED')
rejects(lambda:prepare.verify_request(hw,board,request),'PREPARATION_REQUEST_OR_ACTUAL_CHANGED')
profile._active=False;rejects(profile.check,'RUNTIME_PROFILE_CONTEXT_CLOSED')
assert hw.verify.call_count==8
print(json.dumps(dict(accepted=[1,3],rejected=[0,2,7],configuration_guards=True,hardware=False)))
'''
            result=subprocess.run([sys.executable,'-B','-c',script,str(candidate)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['accepted'],[1,3])

    def test_actual_candidate_state_machine_and_checker_restore(self):
        project=Path(os.environ['LOGICRF_PROJECT_ROOT']);source=project/'envs/h102-sdk-runtime-input'/json.loads((Path(__file__).resolve().parents[1]/'docs/h102-usb-sdk.lock.json').read_text())['input_directory']
        if not source.is_dir():self.skipTest('external pinned H102 SDK source inputs not installed')
        lock=json.loads((Path(__file__).resolve().parents[1]/'docs/h102-usb-sdk.lock.json').read_text())
        with tempfile.TemporaryDirectory(prefix='usb-candidate-',dir=project/'tmp') as root:
            candidate=Path(root)/'code';report=build(candidate,lock,source,offline=True)
            self.assertEqual(set(report['changed']),{'h102_runtime_usb_session.py','h102_tx_v3_guard.py','h102_runtime_usb_recovery.py','h102_runtime_usb_rx.py'})
            script=r'''
import json,struct,sys
sys.path.insert(0,sys.argv[1])
from h102_runtime_usb_session import DigitalSession,DigitalPreparation
from h102_runtime_profile import CONFIG_OFFSETS,TX_OFFSETS
from h102_tx_v3_config import encode
from h102_runtime_usb_recovery import restore_configuration
class Clock:
    now=0.
    def __call__(self):return self.now
class Profile:
    expected_profile=(8000000,4000000)
    def check(self):pass
applied=dict(channels=[dict(channel=ch,sample_rate_num=8000000,sample_rate_den=1,
    rf_bandwidth_hz=4000000,gain_mode=1,center_frequency_hz=2450000000,signed_gain_mdb=-40000) for ch in (1,2)])
tx=encode(applied,42,9,1234,expected_profile=(8000000,4000000))
prepared=DigitalPreparation(0,1,bytes(112),tx,9,1234,41,42,123)
class Board:
    def __init__(self):
        self.words={0x204:9,0x420:0x18}
        for offset,value in zip(TX_OFFSETS,struct.unpack('<64I',tx)):self.words[offset]=value
    def validate(self):pass
    def idle(self):pass
    def sources_idle(self):return True
    def applied(self):return applied
    def read(self,offset):return self.words.get(offset,0)
    def write(self,offset,value):self.words[offset]=value
    def snapshot(self,*args):return {}
clock=Clock();board=Board();session=DigitalSession(Profile(),board,prepared,0,clock=clock)
assert session.deadline is None and session.tx.deadline is None and board.read(0x61c)==0
for n in range(4100):
    clock.now+=1
    session.command(dict(op='renew',token=session.token,seq=n+1))
assert session.sequence==4100
board.write(0x61c,1)
try:session.check();raise AssertionError('checker drift accepted')
except RuntimeError as e:assert 'CHECKER_CHANGED' in str(e)
board.write(0x61c,0);clock.now+=2.001
try:session.check();raise AssertionError('short lease removed')
except TimeoutError:pass
session.stop();assert board.read(0x200)==board.read(0x604)==0
original=dict(rx_hex=bytes(112).hex(),tx_hex=bytes(256).hex(),rx_epoch=8,seed=7,checker=1,prepared=prepared.record())
restored=restore_configuration(board,original,prepared)
assert restored['checker']==1 and restored['seed']==7 and restored['rx_epoch']==8
clock=Clock();board=Board();finite=DigitalSession(Profile(),board,prepared,10,clock=clock)
assert finite.deadline==30 and finite.tx.deadline==30
print(json.dumps(dict(continuous_renewals=4100,short_lease=True,checker_restored=True,finite_deadline=True)))
'''
            result=subprocess.run([sys.executable,'-B','-c',script,str(candidate)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['continuous_renewals'],4100)

# End of file: qa_usb_candidate.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
