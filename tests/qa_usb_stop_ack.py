"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_usb_stop_ack.py
@brief Verify combined RX stop through actual FIFO; reject missing board proof/helper stop or TX requests.
@details Substitute root deployment queries only; no USB/system-service/hardware-data qualification.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import select
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/usb_host_guard.py'
spec=importlib.util.spec_from_file_location('_usb_stop_guard',SCRIPT)
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
PROOF=dict(ok=True,state='STOPPED',rx_control=0,tx_control=0,seq=7)


class UsbStopAckTests(unittest.TestCase):
    """Check actual serial byte ordering; faults must never generate ACK or reuse prior success proofs."""
    def test_actual_fifo_stop_then_observation_then_ack(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);fifo=root/'control.fifo';os.mkfifo(fifo)
            state=dict(running=True,stop_requested=False,error=False);guard.save(root/'state.json',state)
            fd=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
            def receiver():
                self.assertTrue(select.select([fd],[],[],2)[0]);self.assertEqual(os.read(fd,1),b'S')
                self.assertFalse((root/'ack.json').exists())
                guard.save(root/'state.json',dict(state,stop_requested=True))
                self.assertTrue(select.select([fd],[],[],2)[0]);self.assertEqual(os.read(fd,1),b'\n')
                self.assertEqual(json.loads((root/'ack.json').read_text()),PROOF)
            try:
                with ThreadPoolExecutor(1) as pool,patch.object(guard,'request',return_value={}):
                    future=pool.submit(receiver)
                    result=guard.send(root,{},dict(op='stop_ack',proof=PROOF))
                    future.result(3)
                self.assertTrue(result['helper_stop_observed']);self.assertTrue(result['board_stop_verified'])
                with self.assertRaisesRegex(RuntimeError,'DUPLICATE_ACK'):
                    guard.stop_ack(root,state,dict(op='stop_ack',proof=PROOF))
            finally:os.close(fd)

    def test_invalid_proof_and_non_rx_never_write_fifo(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);guard.save(root/'state.json',dict(running=True,stop_requested=False,error=False))
            for proof in ({},dict(PROOF,rx_control=1),dict(PROOF,state='RUNNING')):
                with patch.object(guard,'request',return_value={}):
                    with self.assertRaisesRegex(RuntimeError,'WITHOUT_STOP_PROOF'):
                        guard.send(root,{},dict(op='stop_ack',proof=proof))
            for direction in ('tx','duplex'):
                with patch.object(guard,'request',return_value={'direction':direction}):
                    with self.assertRaises(ValueError):guard.send(root,{},dict(op='stop_ack',proof=PROOF))
            self.assertFalse((root/'ack.json').exists())

    def test_helper_failure_does_not_produce_ack(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);fifo=root/'control.fifo';os.mkfifo(fifo)
            fd=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
            state=dict(running=True,stop_requested=False,error=True);guard.save(root/'state.json',state)
            try:
                with self.assertRaisesRegex(RuntimeError,'STOP_CHILD_FAILED'):
                    guard.stop_ack(root,state,dict(op='stop_ack',proof=PROOF))
                self.assertFalse((root/'ack.json').exists())
            finally:os.close(fd)


if __name__=='__main__':unittest.main()
# End of file: qa_usb_stop_ack.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
