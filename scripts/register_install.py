#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file register_install.py
@brief Register isolated GNU Radio launchers and optional desktop entries for source installations.
@details Verify dependencies, actual module path and bundled runtime before hardware-free mocks.
         Write specified install/workspace/explicit desktop paths only, without hardware or credentials.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.2.2rc2
@date 2026-10-20
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from grc_user_launcher import environment
from install_user import desktop_argument, probe_identity


def main():
    """Verify the installation and register launchers; refuse existing registration or desktop-file overwrites."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',type=Path,required=True)
    parser.add_argument('--gnuradio-prefix',type=Path,required=True)
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--desktop-dir',type=Path,help='Create a desktop entry only when explicitly supplied')
    args=parser.parse_args()
    prefix=args.prefix.resolve();gnu=args.gnuradio_prefix.resolve();project=args.project_root.resolve()
    if not prefix.is_dir():raise FileNotFoundError('CMAKE_INSTALL_REQUIRED')
    if any((prefix/name).exists() for name in ('installation.json','launch.py')):
        raise FileExistsError('INSTALLATION_ALREADY_REGISTERED')
    project.mkdir(parents=True,exist_ok=True)
    identifier='1.2.2rc2-'+hashlib.sha256(str(prefix).encode()).hexdigest()[:12]
    desktop=args.desktop_dir.resolve()/('logicrf-gnuradio-'+identifier+'.desktop') if args.desktop_dir else None
    if desktop and desktop.exists():raise FileExistsError('DESKTOP_ENTRY_ALREADY_EXISTS')
    installation=dict(schema=1,module_prefix=str(prefix),gnuradio_prefix=str(gnu),
        project_root=str(project),installation_id=identifier,version='1.2.2rc2',
        qualification='PUBLIC_SOURCE_CANDIDATE_OFFLINE_VERIFIED')
    env=environment(installation)
    version=subprocess.check_output([str(gnu/'bin/gnuradio-config-info'),'--version'],env=env,text=True).strip()
    if version!='3.10.12.0':raise RuntimeError('REQUIRES_GNU_RADIO_3_10_12_0')
    probe='''import json,pathlib,sys
from unittest.mock import patch
from dataclasses import fields
from gnuradio import gr,blocks,qtgui,logicrf
from gnuradio.logicrf.managed import default_runtime,load_runtime
module=pathlib.Path(logicrf.__file__).resolve()
lock=module.parent/'locks/h102-runtime.lock.json'
runtime=default_runtime(pathlib.Path(sys.argv[1]),lock)
with patch('paramiko.SSHClient',side_effect=AssertionError('SSH forbidden')), patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
    backend=load_runtime(runtime,lock)
    assert [f.name for f in fields(backend.Connection)]==['host','port']
    device=logicrf.make_device(backend='mock',sample_rate=100000,mock_samples=12345)
    graph=gr.top_block();sink=blocks.null_sink(gr.sizeof_gr_complex)
    graph.connect(logicrf.h102_rx_source(device,1),sink);graph.run()
    assert sink.nitems_read(0)==12345 and not device.error()
assert not any(name.startswith(('PySide6','pyqtgraph','h102_iio_demo.gui')) for name in sys.modules)
print('LOGICRF_PREFLIGHT_JSON='+json.dumps(dict(module=str(module),runtime=str(runtime),samples=12345,version=logicrf.__version__)))
'''
    result=subprocess.run(['/usr/bin/python3','-B','-c',probe,str(project)],env=env,
        capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('PREFLIGHT_FAILED:'+result.stderr[-2500:])
    identity=probe_identity(result.stdout)
    if not Path(identity['module']).is_relative_to(prefix):raise RuntimeError('OTHER_MODULE_IMPORTED')
    if not Path(identity['runtime']).is_relative_to(prefix):raise RuntimeError('EXTERNAL_CONTROL_RUNTIME')
    source=Path(__file__).resolve().parent
    shutil.copy2(source/'grc_user_launcher.py',prefix/'launch.py')
    (prefix/'installation.json').write_text(json.dumps(installation,indent=2)+'\n')
    if desktop:
        desktop.parent.mkdir(parents=True,exist_ok=True)
        desktop.write_text('[Desktop Entry]\nType=Application\nName=GNU Radio Companion (LogicRF '+identifier+')\n'
            'Exec=/usr/bin/python3 '+desktop_argument(prefix/'launch.py')+' grc %F\n'
            'Terminal=false\nCategories=Development;Science;\nIcon=gnuradio-grc\n')
    receipt=dict(status='PASS_SOURCE_INSTALL_RUNTIME_AND_NATIVE_MOCK',installation=installation,
        preflight=identity,desktop_entry=str(desktop) if desktop else None,hardware_tested=False)
    (prefix/'install-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__':main()
# End of file: register_install.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
