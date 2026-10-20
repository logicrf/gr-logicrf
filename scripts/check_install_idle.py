#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file check_install_idle.py
@brief Refuse in-place installation over module libraries mapped by any current-user process.
@details Read /proc mappings only, without killing processes/changing permissions; check target after build/tests.
         Even identical ELF rewrites can damage the dynamic loader's private mappings.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.0.0
@date 2026-10-17
"""
import argparse
import json
import os
from pathlib import Path


def users(prefix):
    """Return current-user PIDs mapping exact-prefix module libraries without reading arguments/memory."""
    prefix=Path(prefix).resolve();result=[]
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():continue
        try:
            if process.stat().st_uid!=os.getuid():continue
            paths=[line.split(maxsplit=5)[5].removesuffix(' (deleted)') for line in (process/'maps').read_text().splitlines() if '/libgnuradio-logicrf.so' in line]
            if any(Path(path).is_relative_to(prefix) for path in paths):result.append(int(process.name))
        except (FileNotFoundError,ProcessLookupError,PermissionError):continue
    return sorted(result)


def main():
    """Exit nonzero while in use; callers await natural close without cancelling active flowgraphs."""
    parser=argparse.ArgumentParser();parser.add_argument('prefix',type=Path);args=parser.parse_args()
    busy=users(args.prefix);print(json.dumps(dict(status='INSTALL_PREFIX_IN_USE' if busy else 'INSTALL_PREFIX_IDLE',pids=busy)))
    return int(bool(busy))


if __name__=='__main__':raise SystemExit(main())
# End of file: check_install_idle.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
