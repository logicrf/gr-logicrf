#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file validate_installed.py
@brief Generate/run dual RX or finite mock TX GRC from isolated installs and retain verifiable counts.
@details Never import build outputs; require new output directories; mock only, without hardware/RF.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-16
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess


def main():
    """Check installation identity, GRC exit status and actual dual-channel consumption; preserve failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--direction', choices=('rx', 'tx', 'session'), default='rx')
    args = parser.parse_args()
    out = args.evidence.resolve()
    out.mkdir(parents=True, exist_ok=False)
    prefix = Path(os.environ['LOGICRF_MODULE_PREFIX']).resolve()
    from gnuradio import logicrf
    module_path = Path(logicrf.__file__).resolve()
    if not module_path.is_relative_to(prefix):
        raise RuntimeError('Imported module outside installation prefix')
    version = subprocess.check_output(['gnuradio-config-info', '--version'], text=True).strip()
    if version != '3.10.12.0':
        raise RuntimeError('GNU Radio version mismatch: ' + version)
    graph_name = 'session_control_grc' if args.direction=='session' else 'mock_dual_' + args.direction + '_grc'
    example = prefix / ('share/gr-logicrf/examples/session_control.grc' if args.direction=='session' else 'share/gr-logicrf/examples/mock_dual_' + args.direction + '.grc')
    run = subprocess.run(['grcc', '-o', str(out), str(example)], capture_output=True, text=True)
    (out / 'grcc.log').write_text(run.stdout + run.stderr)
    if run.returncode:
        raise RuntimeError('GRC generation failed')
    generated = out / (graph_name + '.py')
    spec = importlib.util.spec_from_file_location(graph_name, generated)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    graph = getattr(module, graph_name)()
    if args.direction=='session':
        board=graph.h102_device_0
        if board.controller.registered!={'rx':{1},'tx':set()} or graph.control.board is not board:
            raise RuntimeError('GRC_SESSION_DEVICE_BINDING')
        if board.ready() or getattr(board.controller,'client',getattr(board.controller,'link',None)) is not None:raise RuntimeError('GRC_CONSTRUCTOR_HARDWARE_IO')
        board.close()
        result=dict(status='PASS_INSTALLED_SESSION_CONTROL_GRC_CONSTRUCTION',hardware_tested=False,
            generated_sha256=hashlib.sha256(generated.read_bytes()).hexdigest(),installed_module=str(module_path))
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));return
    graph.start()
    try:
        graph.wait()
    finally:
        graph.stop(); graph.wait()
    counts = [graph.sink1.nitems_read(0), graph.sink2.nitems_read(0)]
    if counts != [12345, 12345] or graph.h102_device_0.error():
        raise RuntimeError('GRC output count/error mismatch')
    tx_stats = None
    if args.direction == 'tx':
        tx_stats = [graph.h102_device_0.tx_stats(channel) for channel in (1, 2)]
        for stats, word in zip(tx_stats, (0xc0002000, 0x3000f000)):
            signature = 0
            for _ in range(12345):
                signature = (((signature << 7) | (signature >> 57)) ^ word) & ((1 << 64) - 1)
            if (stats['mock_consumed_samples'] != 12345 or stats['signature'] != signature or
                    stats['discarded_samples'] or not stats['finite_complete']):
                raise RuntimeError('GRC TX count/signature/completion mismatch')
    loaded = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                     if '/libgnuradio-logicrf.so' in line})
    if not loaded or any(not Path(name).resolve().is_relative_to(prefix) for name in loaded):
        raise RuntimeError('Loaded native library outside installation prefix')
    result = dict(status='PASS_P0_MOCK_GRC' if args.direction == 'rx' else 'PASS_P3A_MOCK_TX_GRC',
                  direction=args.direction, tx_stats=tx_stats, gnuradio=version, installed_module=str(module_path),
                  native_libraries=loaded, output_samples=counts, hardware_tested=False,
                  generated_sha256=hashlib.sha256(generated.read_bytes()).hexdigest())
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

# End of file: validate_installed.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
