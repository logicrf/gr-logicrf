#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file grc_user_launcher.py
@brief Launch native Companion or matching Python/grcc from installation receipts.
@details Independent of source trees/shell profiles; paths come from the adjacent receipt; alter this process environment only.
         No device access, credential prompts or standalone SDR windows; gr-logicrf C++ still handles data.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import json
import os
from pathlib import Path
import sys


def environment(installation):
    """Build fixed module/GNU/workspace subprocess environment without inheriting other candidates' Python/GRC paths."""
    module = Path(installation['module_prefix']);gnu = Path(installation['gnuradio_prefix'])
    project = Path(installation['project_root']);state = project/'envs/gnuradio-user'/installation['installation_id']
    env = dict(os.environ)
    env.update(LOGICRF_PROJECT_ROOT=str(project), LOGICRF_MODULE_PREFIX=str(module),
        GNURADIO_PREFIX=str(gnu), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
        PATH=str(gnu/'bin')+':'+env.get('PATH','/usr/bin:/bin'),
        LD_LIBRARY_PATH=str(module/'lib')+':'+str(gnu/'lib'),
        PYTHONPATH=':'.join(map(str,(module/'lib/python3.10/site-packages',gnu/'lib/python3.10/site-packages',
                                    gnu/'lib/python3/dist-packages',Path('/usr/lib/python3/dist-packages')))),
        GRC_BLOCKS_PATH=str(module/'share/gnuradio/grc/blocks')+':'+str(gnu/'share/gnuradio/grc/blocks'),
        XDG_CONFIG_HOME=str(state/'config'),XDG_CACHE_HOME=str(state/'cache'),XDG_STATE_HOME=str(state/'state'),
        GRC_HIER_PATH=str(state/'hier-blocks'))
    for path in (state/'config',state/'cache',state/'state',state/'hier-blocks'):path.mkdir(parents=True,exist_ok=True)
    return env


def main():
    """Direct exec with argument lists for grc/grcc/python/examples; never interpret shell text."""
    root = Path(__file__).resolve().parent
    installed = json.loads((root/'installation.json').read_text())
    if str(root) != installed['module_prefix']:raise RuntimeError('INSTALLATION_MOVED_REINSTALL_REQUIRED')
    mode,*arguments = sys.argv[1:] or ['grc']
    gnu = Path(installed['gnuradio_prefix'])
    commands = {'grc':[str(gnu/'bin/gnuradio-companion')], 'grcc':[str(gnu/'bin/grcc')],
                'python':['/usr/bin/python3'], 'examples':['/usr/bin/xdg-open',str(root/'share/gr-logicrf/examples')]}
    if mode not in commands:raise ValueError('USE grc | grcc | python | examples')
    os.execvpe(commands[mode][0],commands[mode]+arguments,environment(installed))


if __name__ == '__main__':main()
# End of file: grc_user_launcher.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
