"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_runtime_candidate.py
@brief Generate private continuous USB runtimes from per-file-pinned board SDK without modifying the installed release.
@details Check every source SHA and exact replacement count; retain all three locks, process identity,
         two-second permits, DRAIN/ACK and independent recovery. Continuous mode has no task deadline; replies remain bounded.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.7.0
@date 2026-10-16
"""
import hashlib
import json
import os
from pathlib import Path
import stat


def substitute(text,old,new,count=1):
    """Replace audited originals only; count drift fails rather than guessing compatibility with other SDKs."""
    if text.count(old)!=count:raise ValueError('USB_CANDIDATE_SOURCE_ANCHOR:'+old[:100])
    return text.replace(old,new)


def capability_anchor(text,old,new):
    """Accept only pinned SDK cap1 anchors or explicitly paired cap1/cap3 anchors."""
    if text.count(old)==1:return substitute(text,old,new)
    if text.count(old)!=0 or text.count(new)!=1:
        raise ValueError('USB_CANDIDATE_CAPABILITY_ANCHOR')
    return text


def transform(name,text):
    """Return deterministic source transforms; zero seconds is continuous, with hardware/software short permits unchanged."""
    if name=='h102_runtime_profile.py':
        text=capability_anchor(text,'        if self._board.read(0x424) != 1','        if self._board.read(0x424) not in (1, 3)')
        text=capability_anchor(text,'        if board.read(0x424) != 1:','        if board.read(0x424) not in (1, 3):')
    elif name=='h102_runtime_usb_prepare.py':
        text=capability_anchor(text,'            board.read(0x424)!=1):','            board.read(0x424) not in (1,3)):' )
    elif name in ('h102_runtime_usb_session.py','h102_tx_v3_guard.py'):
        text=substitute(text,'not 1 <= seconds <= 1800','not 0 <= seconds <= 1800')
        text=substitute(text,'clock()+seconds+20','clock()+seconds+20 if seconds else None') if name.endswith('session.py') else substitute(text,'clock() + seconds + 20','clock() + seconds + 20 if seconds else None')
        if name.endswith('session.py'):
            text=substitute(text,'self.clock()>=self.deadline','(self.deadline is not None and self.clock()>=self.deadline)')
            text=substitute(text,'board.write(0x61c,1)','board.write(0x61c,0)')
        else:
            text=substitute(text,'self.clock() >= self.deadline','(self.deadline is not None and self.clock() >= self.deadline)')
            text=substitute(text,'board.write(0x61c, 1)','board.write(0x61c, 0)')
            text=substitute(text,'        self.board.validate()\n        self.require_templates()',
                '        self.board.validate()\n        if self.board.read(0x61c)!=0:raise RuntimeError("USB_ARBITRARY_CHECKER_CHANGED")\n        self.require_templates()')
    elif name=='h102_runtime_usb_recovery.py':
        text=substitute(text,"1 if prepared.tx_mask else original['checker']","0 if prepared.tx_mask else original['checker']")
    elif name=='h102_runtime_usb_rx.py':
        text=substitute(text,'session.sequence>=4096','session.sequence>=2**63-1')
        text=substitute(text,"directory/('reply-%04d.json'%session.sequence),reply,exclusive=True", "directory/'reply.json',reply,exclusive=False")
        text=substitute(text,"'--property=RuntimeMaxSec='+str(seconds+70),", "*(['--property=RuntimeMaxSec='+str(seconds+70)] if seconds else []),")
        text=substitute(text,'process.communicate(timeout=seconds+110)','process.communicate(timeout=seconds+110 if seconds else None)')
        text=substitute(text,'seconds not in (10,300,1800)','seconds not in (0,10,300,1800)')
        text=substitute(text,"'--property=RuntimeMaxSec='+str(seconds+125),", "*(['--property=RuntimeMaxSec='+str(seconds+125)] if seconds else []),")
        text=substitute(text,"not 1<=value['seq']<=4096", "not 1<=value['seq']<2**63")
        text=substitute(text,"    path=directory/('reply-%04d.json'%value['seq']);deadline=time.monotonic()+1.5\n    while not path.exists():\n        if time.monotonic()>=deadline:raise TimeoutError('DIGITAL_RX_FORWARD_REPLY_MISSING')\n        time.sleep(.005)\n    profiles.private(path);reply=profiles.read_json(path)\n    if reply['seq']!=value['seq']:raise RuntimeError('DIGITAL_RX_FORWARD_REPLY_SEQUENCE')", "    path=directory/'reply.json';deadline=time.monotonic()+1.5\n    while True:\n        if path.exists():\n            profiles.private(path);reply=profiles.read_json(path)\n            if reply['seq']==value['seq']:break\n            if reply['seq']>value['seq']:raise RuntimeError('DIGITAL_RX_FORWARD_REPLY_SEQUENCE')\n        if time.monotonic()>=deadline:raise TimeoutError('DIGITAL_RX_FORWARD_REPLY_MISSING')\n        time.sleep(.005)")
        text=substitute(text,"deadline=time.monotonic()+launch['seconds']+25\n    while time.monotonic()<deadline:", "deadline=time.monotonic()+launch['seconds']+25 if launch['seconds'] else None\n    while deadline is None or time.monotonic()<deadline:")
        text=substitute(text,'choices=(10,300,1800)','choices=(0,10,300,1800)')
        text=substitute(text,"            qualify=acquisition_qualifier(window,lambda:verify_request(hw,board,request))\n            helper.enable(allocate,qualify)\n            window.observe(board.snapshot())",
            "            if prepared.rx_mask:\n                qualify=acquisition_qualifier(window,lambda:verify_request(hw,board,request))\n                helper.enable(allocate,qualify)\n                window.observe(board.snapshot())\n            else:window=None")
        text=substitute(text,'native=window.record()', 'native=window.record() if window is not None else None',count=2)
    compile(text,name,'exec')
    return text


def radio_transform(name,text):
    """Insert RF extensions at named lifecycle boundaries only; retain manager/main/post digital proofs."""
    if name=='h102_runtime_usb_board.py':
        text=substitute(text,'    def sources_idle(self):\n        paths=',
            '    def sources_idle(self):\n        if getattr(self,"_gr_radio_active",False):\n            import gr_usb_radio\n            return gr_usb_radio.sources_match(self)\n        paths=')
    elif name=='h102_runtime_usb_rx.py':
        text=substitute(text,"            if profile.profile_id!=profile_id:raise RuntimeError('DIGITAL_RX_PROFILE_ID')",
            "            if profile.profile_id!=profile_id:raise RuntimeError('DIGITAL_RX_PROFILE_ID')\n            import gr_usb_radio\n            gr_usb_radio.prepare(hw,board,directory,launch)\n            package=profiles.load_package(CATALOG,profile_id,trusted=True)\n            profile=RuntimeProfile(hw,board,profiles.STATE,profiles.STATE.read_bytes(),package,\n                rx_config.read_applied(hw.phy,expected_profile=profile.expected_profile),\n                tx_config.read_applied(hw.phy,expected_profile=profile.expected_profile))")
        text=substitute(text,"        adapter=PreparationBoard if request['exercise']!='data' else DigitalBoard",
            "        if post:\n            import gr_usb_radio\n            gr_usb_radio.fast_stop(directory,request)\n        adapter=PreparationBoard if request['exercise']!='data' else DigitalBoard")
        text=substitute(text,"            session=DigitalSession(profile,board,prepared,request['seconds'],window)",
            "            import gr_usb_radio\n            gr_usb_radio.activate(board)\n            session=DigitalSession(profile,board,prepared,request['seconds'],window)")
        text=substitute(text,'        finally:\n            if session:',
            '        finally:\n            import gr_usb_radio\n            gr_usb_radio.emergency_stop(hw)\n            if session:')
        text=substitute(text,"        expected_profile=list(session.profile.expected_profile),actor=pin_value(ProcessPin.read(os.getpid()))),exclusive=True)",
            "        expected_profile=list(session.profile.expected_profile),actor=pin_value(ProcessPin.read(os.getpid())),\n        **__import__('gr_usb_radio').ready(session)),exclusive=True)")
        text=substitute(text,"        if not (directory/'request.json').exists():\n            atomic_save",
            "        if not (directory/'request.json').exists() and (directory/'rf-original.json').exists():\n            import gr_usb_radio\n            record=gr_usb_radio.original(directory)\n            recovery=RecoveryManager(scope,record)\n            gr_usb_radio.fast_stop(directory,record)\n            hw,board=open_board(scope,directory,record['identity'])\n            gr_usb_radio.restore(hw,board,directory)\n            atomic_save(directory/'outer-post.json',dict(status='NO_DIGITAL_REQUEST_RF_RESTORED'),exclusive=True)\n            return\n        if not (directory/'request.json').exists():\n            atomic_save")
        text=substitute(text,"        atomic_save(directory/'outer-post.json',dict(status='OUTER_POST_PASS',",
            "        import gr_usb_radio\n        rf_restored=gr_usb_radio.restore(hw,board,directory)\n        atomic_save(directory/'outer-post.json',dict(status='OUTER_POST_PASS',rf_restore=rf_restored,")
    elif name=='h102_runtime_usb_session.py':
        text=substitute(text,"        errors=[]\n        try:\n            self.board.validate()",
            "        import gr_usb_radio\n        gr_usb_radio.emergency_stop(self.board.hw)\n        errors=[]\n        try:\n            self.board.validate()")
    compile(text,name,'exec');return text


def build(directory,lock,source=Path('/usr/local/lib/h102/sdk1/scripts'),*,offline=False,radio_plan=None,extra_sources=None):
    """Verify all originals before creating a directory; production uses root-private /run, offline mode generates files only.

    Return original/generated hashes and modified-file lists. Preserve failed outputs without loading partial results.
    """
    directory=Path(directory);source=Path(source)
    if not offline:
        if os.geteuid()!=0 or directory.parent!=Path('/run') or not directory.name.startswith('h102-gr-usb-code-'):
            raise ValueError('USB_CANDIDATE_ROOT_PRIVATE_RUN')
        for parent in (source,*source.parents):
            info=parent.lstat()
            if info.st_uid!=0 or info.st_mode&0o022 or not stat.S_ISDIR(info.st_mode):raise ValueError('USB_SDK_UNTRUSTED_ROOT')
    actual={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.py')}
    if actual!=lock['files']:raise ValueError('USB_SDK_SOURCE_LOCK_MISMATCH')
    outputs={}
    for name in sorted(actual):
        path=source/name;info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or not offline and (info.st_uid!=0 or info.st_mode&0o022):
            raise ValueError('USB_SDK_UNTRUSTED_FILE:'+name)
        text=transform(name,path.read_text())
        if radio_plan is not None:text=radio_transform(name,text)
        outputs[name]=text.encode()
    if radio_plan is not None:
        if set(extra_sources or {})!={'gr_usb_radio.py','board_tx.py','board_radio.py','tx_cyclic.py'}:
            raise ValueError('USB_RADIO_EXACT_EXTRA_SOURCES')
        for name,raw in extra_sources.items():
            compile(raw,name,'exec');outputs[name]=raw.encode() if isinstance(raw,str) else raw
        outputs['gr_usb_radio_plan.py']=('PLAN = '+repr(radio_plan)+'\n').encode()
    directory.mkdir(mode=0o700,parents=False,exist_ok=False)
    for name,raw in outputs.items():
        path=directory/name;path.write_bytes(raw);path.chmod(0o600)
    builder=Path(__file__).read_bytes();(directory/'gr_usb_candidate_builder.py').write_bytes(builder)
    (directory/'gr_usb_candidate_builder.py').chmod(0o600)
    report=dict(schema=1,original=actual,candidate={n:hashlib.sha256(v).hexdigest() for n,v in outputs.items()},
        builder_sha256=hashlib.sha256(builder).hexdigest(),changed=[n for n,v in outputs.items() if actual.get(n)!=hashlib.sha256(v).hexdigest()],
        RF_TX_requested=bool(radio_plan and radio_plan['rf_enabled']),continuous=True,tx_checker=0,short_lease_unchanged=True)
    (directory/'candidate-build.json').write_text(json.dumps(report,indent=2)+'\n');(directory/'candidate-build.json').chmod(0o600)
    return report

# End of file: usb_runtime_candidate.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
