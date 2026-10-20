#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file install_user.py
@brief Install fully SHA-verified module/control sources to a new directory and register Companion desktop entries.
@details Never overwrite active versions or access H102/install credentials; preserve failed GNU/system prerequisite checks.
         Launchers manage environment paths so ordinary users need no development-shell setup or Linux password.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys


def sha(path):
    """Stream complete hashes without logging file contents or credentials."""
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1<<20),b''):result.update(data)
    return result.hexdigest()


def tree(root):
    """Allow regular files/root-contained relative file symlinks; reject directory links, devices and escaping targets."""
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            link = os.readlink(path)
            if Path(link).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError('PACKAGE_LINK_OUTSIDE_OR_NOT_FILE:'+relative)
            result[relative] = dict(link=link)
        elif stat.S_ISREG(mode):result[relative] = dict(sha256=sha(path),bytes=path.stat().st_size)
        elif not stat.S_ISDIR(mode):raise ValueError('PACKAGE_NOT_ORDINARY:'+relative)
    return result


def verify(root, expected):
    """Require exact file set/content; reject external manifest paths."""
    if any(Path(p).is_absolute() or '..' in Path(p).parts or Path(p).as_posix()!=p for p in expected):
        raise ValueError('PACKAGE_MANIFEST_PATH')
    if tree(root) != expected:raise ValueError('PACKAGE_TREE_SHA_OR_SET_MISMATCH')


def desktop_argument(value):
    """Encode literal arguments using Desktop Entry Exec quoting, without a shell."""
    if any(c in str(value) for c in '\n\r\x00'):raise ValueError('DESKTOP_CONTROL_CHARACTER')
    quoted='"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$').replace('%','%%')+'"'
    return quoted.replace('\\','\\\\')


def probe_identity(stdout):
    """Libraries may log to stdout; accept one explicitly marked current result and retain ambiguity errors."""
    prefix='LOGICRF_PREFLIGHT_JSON='
    values=[json.loads(line[len(prefix):]) for line in stdout.splitlines() if line.startswith(prefix)]
    if len(values)!=1:raise RuntimeError('PREFLIGHT_RESULT_MISSING_OR_AMBIGUOUS')
    return values[0]


def main():
    """Install to a new target and run actual import/mock/desktop checks; failed leftovers are never reported as success."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--package',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--gnuradio-prefix',type=Path,required=True)
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--desktop-dir',type=Path,default=Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'applications')
    args = parser.parse_args();package = args.package.resolve();payload = package/'payload'
    manifest = json.loads((package/'package.json').read_text())
    if manifest['schema'] != 1:raise ValueError('PACKAGE_SCHEMA')
    if sha(Path(__file__).resolve())!=manifest['installer_sha256']:raise ValueError('INSTALLER_SOURCE_MISMATCH')
    verify(payload,manifest['files'])
    if (sys.version_info[:2]!=(3,10) or platform.system()!='Linux' or platform.machine()!='x86_64'):
        raise RuntimeError('REQUIRES_LINUX_X86_64_PYTHON_3_10')
    destination = args.destination.absolute();gnu = args.gnuradio_prefix.resolve();project = args.project_root.resolve()
    if destination.exists() or destination.is_symlink():raise FileExistsError('NEW_INSTALLATION_DIRECTORY_REQUIRED')
    if not project.is_dir():raise FileNotFoundError('PROJECT_ROOT_MUST_EXIST')
    lock=json.loads((payload/'module/lib/python3.10/site-packages/gnuradio/logicrf/locks/h102-runtime.lock.json').read_text())
    if (not re.fullmatch('[a-f0-9]{40}',manifest.get('gui_commit','')) or
            lock['gui_commit']!=manifest['gui_commit'] or lock['runtime_files']!=manifest['runtime_files']):
        raise ValueError('CONTROL_RUNTIME_PACKAGE_LOCK_MISMATCH')
    runtime_name=lock.get('runtime_directory',manifest['gui_commit'][:7])
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._-]*',runtime_name):raise ValueError('CONTROL_RUNTIME_DIRECTORY')
    runtime = project/'envs/h102-control-runtime'/runtime_name
    runtime_files = manifest['runtime_files']
    if runtime.exists():
        if runtime.is_symlink() or any(not (runtime/p).is_file() or sha(runtime/p)!=digest for p,digest in runtime_files.items()):
            raise RuntimeError('EXISTING_CONTROL_RUNTIME_MISMATCH')
    identifier = manifest['installation_id']
    if not identifier or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in identifier):
        raise ValueError('INSTALLATION_ID')
    desktop = args.desktop_dir.absolute()/('logicrf-gnuradio-'+identifier+'.desktop')
    if desktop.exists():raise FileExistsError('DESKTOP_ENTRY_ALREADY_EXISTS')
    sys.dont_write_bytecode=True
    spec = importlib.util.spec_from_file_location('_logicrf_user_launcher',payload/'grc_user_launcher.py')
    launcher = importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    preflight = dict(module_prefix=str(payload/'module'),gnuradio_prefix=str(gnu),project_root=str(project),installation_id=identifier)
    env = launcher.environment(preflight)
    version = subprocess.check_output([str(gnu/'bin/gnuradio-config-info'),'--version'],env=env,text=True).strip()
    if version!='3.10.12.0':raise RuntimeError('REQUIRES_GNU_RADIO_3_10_12_0_QTGUI_FOUND:'+version)
    probe = '''import json, pathlib
from gnuradio import gr,blocks,analog,filter,fft,qtgui,logicrf
import gi, numpy, paramiko
gi.require_version('Gtk','3.0');gi.require_version('PangoCairo','1.0')
gi.require_foreign('cairo','Context')
from gi.repository import Gtk,PangoCairo
from PyQt5 import QtCore
from h102_control.client import Connection
board=logicrf.make_device(backend='mock',sample_rate=100000,mock_samples=12345)
graph=gr.top_block();sink=blocks.null_sink(gr.sizeof_gr_complex)
graph.connect(logicrf.h102_rx_source(board,1),sink);graph.run()
assert sink.nitems_read(0)==12345 and not board.error()
assert set(Connection('192.0.2.1').__dict__)=={'host','port'}
print('LOGICRF_PREFLIGHT_JSON='+json.dumps(dict(module=str(pathlib.Path(logicrf.__file__).resolve()),version=logicrf.__version__,samples=12345)))
'''
    result = subprocess.run(['/usr/bin/python3','-B','-c',probe],env=env,capture_output=True,text=True,timeout=20,cwd=payload)
    if result.returncode:raise RuntimeError('DEPENDENCY_OR_MODULE_PREFLIGHT_FAILED:'+result.stderr[-2500:])
    identity = probe_identity(result.stdout)
    if not Path(identity['module']).is_relative_to(payload/'module'):raise RuntimeError('PREFLIGHT_IMPORTED_OTHER_MODULE')
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(payload/'module',destination,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    expected = {p.removeprefix('module/'):v for p,v in manifest['files'].items() if p.startswith('module/')}
    verify(destination,expected)
    if not runtime.exists():
        runtime.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(payload/'runtime',runtime)
    if any(sha(runtime/p)!=digest for p,digest in runtime_files.items()):raise RuntimeError('RUNTIME_INSTALL_READBACK')
    shutil.copy2(payload/'grc_user_launcher.py',destination/'launch.py')
    installation = dict(preflight,module_prefix=str(destination.resolve()),schema=1,package_sha256=sha(package/'package.json'),
        source_commit=manifest['module_commit'],qualification=manifest['qualification'],version=identity['version'])
    (destination/'installation.json').write_text(json.dumps(installation,indent=2)+'\n')
    (destination/'launch.py').chmod(0o755)
    final = subprocess.run(['/usr/bin/python3',str(destination/'launch.py'),'python','-B','-c',probe],
                           capture_output=True,text=True,timeout=20,cwd=destination)
    if final.returncode:raise RuntimeError('INSTALLED_MODULE_PREFLIGHT_FAILED:'+final.stderr[-2500:])
    installed_probe=probe_identity(final.stdout)
    if not Path(installed_probe['module']).is_relative_to(destination.resolve()):raise RuntimeError('INSTALLED_OTHER_MODULE')
    desktop.parent.mkdir(parents=True,exist_ok=True)
    label=identity['version']+' '+manifest['delivery_commit'][:8]
    desktop.write_text('[Desktop Entry]\nType=Application\nName=GNU Radio Companion (LogicRF '+label+')\n'
        'Name[zh_CN]=GNU Radio Companion（LogicRF '+label+'）\n'
        'Exec=/usr/bin/python3 '+desktop_argument(destination/'launch.py')+' grc %F\n'
        'Terminal=false\nCategories=Development;Science;\nIcon=gnuradio-grc\n')
    receipt = dict(status='PASS_USER_INSTALL_AND_NATIVE_MOCK',installation=installation,desktop_entry=str(desktop),
                   module_files=len(expected),normal_device_login_required=False,hardware_tested=False,
                   native_mock=installed_probe,
                   dependency_diagnostics=[line for line in (result.stdout+final.stdout).splitlines()
                       if not line.startswith('LOGICRF_PREFLIGHT_JSON=')])
    (destination/'install-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__ == '__main__':main()
# End of file: install_user.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
