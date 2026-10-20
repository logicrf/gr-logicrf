"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_broker_observation.py
@brief Check fixed observation modules' per-request validation and same-UID boundaries through actual UNIX sockets.
@details Hardware-free guard substitute; test actual broker loading/protocol/drift rejection/dispatch.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from h102_control.protocol import encode_frame,receive_frame

spec=importlib.util.spec_from_file_location('tested_usb_broker',Path(__file__).resolve().parents[1]/'scripts/usb_host_broker.py')
broker=importlib.util.module_from_spec(spec);spec.loader.exec_module(broker)


class ObservationTests(unittest.TestCase):
    """Revalidate deployment per request; cached objects cannot assume changed files' new identities."""
    def test_fixed_observation_rechecks_and_rejects_drift_or_bad_peer(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);guard=root/'guard.py';manifest=root/'deployment.json'
            manifest.write_text('{}')
            guard.write_text('calls=[]\ndef deployment():\n calls.append("validate");return {}\ndef status(path,d):\n calls.append("status");return {"live":True}\ndef send(path,d,p):\n calls.append("send");return p\n')
            with broker.Server(str(root/'control.sock'),guard,os.getuid(),inline_observation=True) as server:
                worker=Thread(target=server.serve_forever);worker.start()
                def request(action='status',payload=None):
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                        peer.settimeout(3);peer.connect(str(root/'control.sock'))
                        peer.sendall(encode_frame(dict(action=action,session='h102-gr-usb-'+'a'*32,value=payload)))
                        with peer.makefile('rb') as reader:return receive_frame(reader)
                try:
                    self.assertTrue(request()['result']['live']);self.assertTrue(request()['result']['live'])
                    self.assertEqual(request('send',{'op':'stop'})['result'],{'op':'stop'})
                    self.assertEqual(server.observation_guard.calls.count('validate'),4)
                    baseline=list(server.observation_guard.calls)
                    server.uid=os.getuid()+1;self.assertFalse(request()['ok']);server.uid=os.getuid()
                    manifest.write_text('{"drift":true}');self.assertIn('OBSERVATION_PIN',request()['error']);manifest.write_text('{}')
                    original=guard.read_text();guard.write_text(original+'# drift\n');self.assertIn('OBSERVATION_PIN',request()['error']);guard.write_text(original)
                    self.assertFalse(request('arbitrary')['ok']);self.assertEqual(server.observation_guard.calls,baseline)
                finally:server.shutdown();worker.join(3)


if __name__=='__main__':unittest.main()
# End of file: qa_usb_broker_observation.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
