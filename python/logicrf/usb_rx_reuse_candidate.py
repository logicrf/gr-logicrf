"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file usb_rx_reuse_candidate.py
@brief Generate pinned USB RX runtimes retaining GNSS messages and reacquiring time through interlocks.
@details Extend a new entry only, preserving the old generator/SDK; reject exact source-anchor mismatches.
         Record new configuration/time epochs separately; disable time during template writes and track recovery in ledgers.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import hashlib
import json
from pathlib import Path

if __package__:
    from . import usb_runtime_candidate as legacy
else:
    import usb_runtime_candidate as legacy


def transform(name, text):
    """Transform deterministic continuous/RF output only; never modify the installed source."""
    sub = legacy.substitute
    # The shared generator recognizes only the audited cold ABIs 1 and 3.
    # RX hot metadata and its narrow write interface remain exclusive here.
    if name == 'h102_runtime_usb_board.py':
        text = sub(text, '    def validate(self):\n        super().validate()',
            '    def write_metadata(self, offset, value):\n'
            '        """Fixed hot-metadata ABI only; restricted writes verify actual source-idle state and readback."""\n'
            '        from gr_usb_reuse import write_metadata\n'
            '        return write_metadata(self, offset, value)\n\n'
            '    def validate(self):\n        super().validate()')
    elif name == 'h102_runtime_usb_session.py':
        text = sub(text, '        self.profile,self.board,self.prepared,self.clock=profile,board,prepared,clock',
            "        self.time_epoch=time_window.record()['time_epoch'] if prepared.rx_mask else prepared.epoch\n"
            '        self.profile,self.board,self.prepared,self.clock=profile,board,prepared,clock')
        text = sub(text, "record['time_epoch']!=prepared.epoch", "record['time_epoch']!=self.time_epoch")
        text = sub(text, 'self.board.read(0x408)!=self.prepared.epoch', 'self.board.read(0x408)!=self.time_epoch')
        # READY+START is one serial RX transaction: validate actual hardware
        # before binding and after DATA, with the real native window before RUN.
        # The legacy separate commands retain their original semantics.
        text = sub(text, "('receiver_ready','sender_ready')",
                   "('receiver_ready','sender_ready','receiver_ready_start')")
        text = sub(text, "receiver=op=='receiver_ready';mask=",
                   "if op=='receiver_ready_start' and (not self.prepared.rx_mask or self.prepared.tx_mask):raise RuntimeError('RUNTIME_COMBINED_READY_RX_ONLY')\n                receiver=op in ('receiver_ready','receiver_ready_start');mask=")
        text = sub(text, "            elif op in ('drain','finish_drain','arm'):",
                   "            if op in ('drain','finish_drain','arm'):")
        text = sub(text, "            elif op=='start':",
                   "            elif op in ('start','receiver_ready_start'):")
        text = sub(text, "            else:raise ValueError('RUNTIME_DIGITAL_UNKNOWN_OPERATION')",
                   "            elif not ready:raise ValueError('RUNTIME_DIGITAL_UNKNOWN_OPERATION')")
        # Pure RX construction has no DATA writes; its final self.check still
        # validates the actual profile before the object can be published.
        text = sub(text, '        profile.check();board.validate();board.idle();self._ownership()',
                   '        if prepared.tx_mask:profile.check()\n        board.validate();board.idle();self._ownership()')
        # Board.snapshot(mask, epoch) uses epoch for transport ownership at
        # 0x204/0x21c. Its native time record is read independently from 0x408;
        # keep prepared.epoch here when the retained time epoch differs.
    elif name == 'h102_runtime_usb_rx.py':
        text = sub(text, 'seconds=seconds,prepared=session.prepared.record(),',
                   'seconds=seconds,prepared=session.prepared.record(),rx_reuse_ready_start_v1=True,')
        text = sub(text, 'from h102_runtime_usb_recovery import stop_configuration, restore_configuration',
                   'from gr_usb_reuse import stop_configuration, restore_configuration, restore_initial_radio, owns_time, ReusableRxSession')
        text = sub(text, "            session=DigitalSession(profile,board,prepared,request['seconds'],window)",
                   "            session=DigitalSession(profile,board,prepared,request['seconds'],window)\n"
                   "            session=ReusableRxSession(session,directory,request)")
        text = sub(text, '            hw.verify();hw.idle();verify_request(hw,board,request)',
                   '            restore_initial_radio(hw,directory,request)\n'
                   '            hw.verify();hw.idle();verify_request(hw,board,request)')
        text = sub(text, "board.read(0x408)!=prepared.epoch or state.get('gnss_allocated_epoch')!=prepared.epoch",
                   "not owns_time(board,directory,prepared.epoch)")
        text = sub(text, "if request['exercise']=='data':serve(session,directory,request['seconds'])",
                   "if request['exercise']=='data':\n                serve(session,directory,request['seconds'])\n                window=session.time_window")
        text = sub(text, "            if not select.select([descriptor],[],[],.05)[0]:continue",
                   "            phase=session.preparing\n            wait=.001 if phase and phase['stage']=='building' else .025 if phase else .05\n            if not select.select([descriptor],[],[],wait)[0]:continue")
        text = sub(text, "deadline=time.monotonic()+1.5", "deadline=time.monotonic()+(8 if value.get('op')=='prepare_epoch' else 1.5)")
    compile(text, name, 'exec')
    return text


def build(directory, lock, source=Path('/usr/local/lib/h102/sdk1/scripts'), *,
          offline=False, radio_plan=None, extra_sources=None, reuse_source):
    """Combine existing generators under a fixed RX-only plan; write the full closure and freeze output hashes."""
    if (not radio_plan or radio_plan.get('rf_enabled') is not False or
            radio_plan.get('tx_channels') or radio_plan.get('rx_channels') not in ([1], [2], [1, 2])):
        raise ValueError('USB_REUSE_RX_ONLY')
    compile(reuse_source, 'gr_usb_reuse.py', 'exec')
    original = legacy.radio_transform
    def combined(name, text):
        return transform(name, original(name, text))
    legacy.radio_transform = combined
    try:
        report = legacy.build(directory, lock, source, offline=offline,
                              radio_plan=radio_plan, extra_sources=extra_sources)
    finally:
        legacy.radio_transform = original
    directory = Path(directory)
    for name, raw in (('gr_usb_reuse.py', reuse_source.encode()),
                      ('gr_usb_reuse_builder.py', Path(__file__).read_bytes())):
        path = directory/name;path.write_bytes(raw);path.chmod(0o600)
    report['reusable_rx'] = True
    report['time_epoch_semantics'] = 'cap1 cold qualification; cap3 stopped RX metadata commit retains actual continuously observed native time'
    report['candidate'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(directory.glob('*.py'))}
    report['changed'] = [name for name, digest in report['candidate'].items()
                         if report['original'].get(name) != digest]
    (directory/'candidate-build.json').write_text(json.dumps(report, indent=2)+'\n')
    return report

# End of file: usb_rx_reuse_candidate.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
