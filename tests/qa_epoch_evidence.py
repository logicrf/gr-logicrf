"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_epoch_evidence.py
@brief Actual tar/disk/fsync tests for exact USB generation retirement; drift/persistence failures preserve originals.
@details systemd/USB/root identity are substitutes; archive/client restoration actually execute without hardware qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
archive=load('tested_host_archive',ROOT/'scripts/usb_host_archive.py')
client=load('tested_epoch_evidence',ROOT/'python/logicrf/epoch_evidence.py')


class EvidenceTests(unittest.TestCase):
    """Compare actual originals/restored files rather than assuming archive-call success proves integrity."""
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.directory=self.root/('h102-gr-usb-'+'a'*32);self.directory.mkdir(mode=0o700)
        self.output=self.root/'output';self.output.mkdir(mode=0o700)
        final={'main.json':{'passed':True},'post.json':{'passed':True},
               'state.json':{'running':False},'native':{'ExecMainStatus':'0'}}
        for name,value in [('final.json',final),('main.json',final['main.json']),
                           ('post.json',final['post.json']),('quota.json',{'original':64})]:
            path=self.directory/name;path.write_text(json.dumps(value));path.chmod(0o600)
        os.mkfifo(self.directory/'control.fifo',0o600)
        quota=self.root/'quota';quota.write_text('64')
        self.guard=SimpleNamespace(request=Mock(return_value={'host':{'node':'fixture'}}),
            unused=Mock(),locked=lambda d:nullcontext(),QUOTA=quota)
        self.systemd=patch.object(archive.subprocess,'run',return_value=SimpleNamespace(stdout='inactive\n'))
        self.systemd.start()
    def tearDown(self):self.systemd.stop();self.temp.cleanup()
    def endpoint(self,action,value=None,timeout=None):
        self.assertIn(action,('evidence_open','evidence_retire'))
        return archive.evidence(self.directory,{},self.guard,value)
    def test_full_restore_then_exact_retire_including_fifo_metadata(self):
        controller=SimpleNamespace(run_output=self.output,_host=self.endpoint)
        result=client.retire_host(controller,0)
        self.assertEqual(result['status'],'RETIRED_EXACT_VERIFIED')
        self.assertFalse(self.directory.exists())
        restored=self.output/'host-archives/00000000/restored'
        self.assertTrue((restored/'final.json').exists());self.assertFalse((restored/'control.fifo').exists())
    def test_changed_original_or_wrong_sha_zero_delete(self):
        reply=self.endpoint('evidence_open')
        for update in ({'archive_sha256':'0'*64},{'manifest':{}}):
            value={k:reply[k] for k in ('archive_sha256','manifest')};value.update(update)
            with self.assertRaisesRegex(RuntimeError,'ORIGINAL_CHANGED'):self.endpoint('evidence_retire',value)
            self.assertTrue(self.directory.exists())
        (self.directory/'unexpected').write_text('drift');(self.directory/'unexpected').chmod(0o600)
        with self.assertRaisesRegex(RuntimeError,'ORIGINAL_CHANGED'):
            self.endpoint('evidence_retire',{k:reply[k] for k in ('archive_sha256','manifest')})
        self.assertTrue(self.directory.exists())
    def test_active_unit_or_symlink_zero_delete(self):
        with patch.object(archive.subprocess,'run',return_value=SimpleNamespace(stdout='active\n')):
            with self.assertRaisesRegex(RuntimeError,'UNIT_ACTIVE'):self.endpoint('evidence_open')
        (self.directory/'link').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(RuntimeError,'MEMBER'):self.endpoint('evidence_open')
        self.assertTrue(self.directory.exists())
    def test_durable_failure_never_confirms_retire(self):
        caller=Mock(side_effect=self.endpoint)
        controller=SimpleNamespace(run_output=self.output,_host=caller)
        with patch.object(client.os,'fsync',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):client.retire_host(controller,0)
        self.assertEqual([c.args[0] for c in caller.call_args_list],['evidence_open'])
        self.assertTrue(self.directory.exists())
    def test_corrupt_wire_never_confirms_retire(self):
        reply=self.endpoint('evidence_open');reply['archive_sha256']='0'*64
        caller=Mock(return_value=reply)
        with self.assertRaisesRegex(RuntimeError,'WIRE'):
            client.retire_host(SimpleNamespace(run_output=self.output,_host=caller),0)
        caller.assert_called_once();self.assertTrue(self.directory.exists())


class AuditTests(unittest.TestCase):
    """Actual fsync/SHA still precede retirement with either wire layout."""
    def exercise(self, *, inline=True, size=123, fault=None):
        import base64,hashlib
        raw=(b'audit line\n'*((size+10)//11))[:size];identifier='a'*32;calls=[]
        def endpoint(op,**value):
            calls.append(op)
            if op=='audit_open':
                reply=dict(id=identifier,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
                if inline and raw:
                    reply['first_chunk']=dict(id=identifier,index=1 if fault=='index' else 0,
                        data=base64.b64encode(raw[:12288]).decode())
                return dict(audit=reply)
            if op=='audit_chunk':
                index=value['index'];return dict(audit=dict(id=identifier,index=index,
                    data=base64.b64encode(raw[index*12288:(index+1)*12288]).decode()))
            self.assertEqual(op,'audit_retire')
            saved=next(Path(temporary).rglob('writes.jsonl')).read_bytes()
            self.assertEqual(saved,raw);self.assertEqual(value['sha256'],hashlib.sha256(saved).hexdigest())
            return dict(audit=dict(status='RETIRED_EXACT_VERIFIED',sha256=value['sha256']))
        with TemporaryDirectory() as temporary:
            controller=SimpleNamespace(run_output=Path(temporary),_command=endpoint)
            if fault:
                context=patch.object(client.os,'fsync',side_effect=OSError('disk failure')) if fault=='fsync' else nullcontext()
                with context,self.assertRaises((OSError,RuntimeError)):
                    client.checkpoint_audit(controller,0)
                self.assertNotIn('audit_retire',calls)
            else:
                client.checkpoint_audit(controller,0)
                expected=(len(raw)+12287)//12288-(1 if inline and raw else 0)
                self.assertEqual(calls.count('audit_chunk'),expected)
                self.assertEqual(calls[-1],'audit_retire')
    def test_inline_and_legacy_empty_small_and_multichunk(self):
        for inline in (False,True):
            for size in (0,123,32768):
                with self.subTest(inline=inline,size=size):self.exercise(inline=inline,size=size)
    def test_bad_inline_identity_and_fsync_never_retire(self):
        for fault in ('index','fsync'):
            with self.subTest(fault=fault):self.exercise(fault=fault)


class WholeArchiveTests(unittest.TestCase):
    """Final ownership archives also require fsync/restored SHA before board acknowledgement; verify real files."""
    def exercise(self,fail):
        import base64,hashlib,io,tarfile
        from gnuradio.logicrf.runtime_archive import retire
        data=b'actual archive fixture\n';name='/run/h102-runtime-usb-data-c16-gr-'+'b'*32+'/post.json'
        manifest={name:dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())}
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as packed:
            for path,raw in [('manifest.json',json.dumps(manifest).encode()),(name.lstrip('/'),data)]:
                member=tarfile.TarInfo(path);member.size=len(raw);packed.addfile(member,io.BytesIO(raw))
        raw=stream.getvalue();digest=hashlib.sha256(raw).hexdigest();calls=[]
        def endpoint(op,value,**kwargs):
            calls.append(op)
            if op.endswith('open'):return dict(bytes=len(raw),chunk_size=65536,archive_sha256=digest)
            if op.endswith('chunk'):return dict(index=0,data=base64.b64encode(raw).decode())
            return dict(status='RETIRED_EXACT_VERIFIED')
        with TemporaryDirectory() as temporary,patch('h102_control.client.Client') as connector:
            connector.return_value.request.side_effect=endpoint
            controller=SimpleNamespace(connection=None,api_session_id='b'*32,run_output=Path(temporary),_save=Mock())
            if fail:
                with patch('os.fsync',side_effect=OSError('fixture fsync failure')):
                    with self.assertRaises(OSError):retire(controller)
                self.assertNotIn('usb.evidence.retire',calls)
            else:
                retire(controller)
                self.assertEqual(calls[-1],'usb.evidence.retire')
                proof=json.loads((Path(temporary)/'runtime-archive-verified.json').read_text())
                self.assertTrue(proof['fsync_verified'])
                self.assertEqual((Path(temporary)/'runtime-archive/restored'/name.lstrip('/')).read_bytes(),data)
    def test_final_archive_fsync_and_restore(self):self.exercise(False)
    def test_final_archive_fsync_failure_no_confirmation(self):self.exercise(True)


if __name__=='__main__':unittest.main()
# End of file: qa_epoch_evidence.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
