#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_iio_protocol.py
@brief Drive actual libiio 0.23 networking with local IIOD replies to verify variable IQ lengths/rejection boundaries.
@details Fixed loopback, no credentials/board operations; native receivers must not pad/drop valid short blocks.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.2.1
@date 2026-10-16
"""
import argparse
from collections import Counter
import json
import socketserver
import struct
import subprocess
from threading import Thread

XML=b'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE context [
<!ELEMENT context (device*)>
<!ATTLIST context name CDATA #REQUIRED version-major CDATA #IMPLIED version-minor CDATA #IMPLIED version-git CDATA #IMPLIED>
<!ELEMENT device (channel*)><!ATTLIST device id CDATA #REQUIRED name CDATA #IMPLIED>
<!ELEMENT channel (scan-element)><!ATTLIST channel id CDATA #REQUIRED type CDATA #REQUIRED>
<!ELEMENT scan-element EMPTY><!ATTLIST scan-element index CDATA #REQUIRED format CDATA #REQUIRED>
]>
<context name="local" version-major="0" version-minor="23" version-git="abcdefg">
<device id="iio:device0" name="axi-adrv9002-rx-lpc">
<channel id="voltage0_i" type="input"><scan-element index="0" format="le:S16/16&gt;&gt;0"/></channel>
<channel id="voltage0_q" type="input"><scan-element index="1" format="le:S16/16&gt;&gt;0"/></channel>
</device></context>'''


class Server(socketserver.ThreadingTCPServer):
    """Short-lived fixed-loopback simulator only; never proxy external devices."""
    allow_reuse_address=True
    daemon_threads=True


class Handler(socketserver.StreamRequestHandler):
    """Parse IIOD text on one connection; memory-generated RAMP supplies binary data only."""
    def handle(self):
        self.request.settimeout(15)
        while True:
            try:line=self.rfile.readline().decode().strip()
            except (OSError,UnicodeDecodeError):return
            if not line:return
            server=self.server;server.commands.append(line)
            try:
                if line=='PRINT':
                    self.wfile.write(str(len(XML)).encode()+b'\n'+XML+b'\n')
                elif line=='ZPRINT':self.wfile.write(b'-22\n')
                elif line=='VERSION':self.wfile.write(b'0.23.abcdefg\n')
                elif line=='EXIT':return
                elif line.startswith(('TIMEOUT ','SET ','OPEN ','CLOSE ')):
                    self.wfile.write(b'0\n')
                elif line.startswith('READBUF '):
                    requested=int(line.split()[-1]);server.refills+=1
                    # 34 complete warmup blocks; then vary delivery chunking without changing the RAMP sequence.
                    n=4 if server.mode=='warm-short' else requested//4
                    if server.refills>34:
                        if server.mode=='short':n=(1,3,7,16)[(server.refills-35)%4]
                        elif server.mode=='unaligned':
                            self.wfile.write(b'5\n00000003\nabcde0\n');self.wfile.flush();continue
                        elif server.mode=='empty-forever' or (server.mode=='empty-once' and server.refills==35):
                            self.wfile.write(b'0\n');self.wfile.flush();continue
                    raw=b''.join(struct.pack('<HH',(server.index+i)&65535,(server.index+i)&65535) for i in range(n))
                    server.index+=n
                    self.wfile.write(str(len(raw)).encode()+b'\n00000003\n'+raw)
                    if len(raw)<requested:self.wfile.write(b'0\n')
                else:
                    server.errors.append('unsupported:'+line);self.wfile.write(b'-22\n')
                self.wfile.flush()
            except (OSError,ValueError):return


def run(executable,mode):
    """Exchange actual loopback commands/masks/bytes and await C++ cleanup; retain commands for failure analysis."""
    with Server(('127.0.0.102',30431),Handler) as server:
        server.mode=mode;server.refills=0;server.index=0;server.errors=[];server.commands=[]
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        try:result=subprocess.run([str(executable)],capture_output=True,text=True,timeout=10)
        finally:server.shutdown();thread.join()
        record=dict(mode=mode,returncode=result.returncode,stderr=result.stderr,
                    commands=server.commands[:8]+server.commands[-8:],command_count=len(server.commands),
                    command_counts=dict(Counter(x.split()[0] for x in server.commands)),
                    server_errors=server.errors,stdout=result.stdout)
        try:record['reader']=json.loads(result.stdout)
        except json.JSONDecodeError:pass
        return record


def main():
    """CLI supports old-behavior reproduction and quick checks of the corrected contract."""
    p=argparse.ArgumentParser();p.add_argument('--reader',required=True)
    p.add_argument('--mode',choices=('full','short','warm-short','unaligned','empty-once','empty-forever','suite'),default='short')
    p.add_argument('--observe',action='store_true');args=p.parse_args()
    modes=('full','short','warm-short','unaligned','empty-once','empty-forever') if args.mode=='suite' else (args.mode,)
    passed=True
    for mode in modes:
        r=run(args.reader,mode);print(json.dumps(r,ensure_ascii=False),flush=True)
        if args.observe:continue
        v=r.get('reader',{});success=(not r['server_errors'] and r['returncode']==0)
        if mode=='unaligned':success=success and 'IIO_BUFFER_GEOMETRY' in v.get('error','') and v.get('samples')==0 and v.get('end')==0
        elif mode=='empty-forever':success=success and v.get('error')=='IIO_EMPTY_REFILL_DEADLINE' and v.get('samples')==0 and v.get('end')==0
        else:success=success and v.get('samples')==37 and v.get('ramp_errors')==0 and not v.get('error') and v.get('end')==1
        passed=passed and success
    return 0 if passed else 1



if __name__=='__main__':raise SystemExit(main())

# End of file: qa_iio_protocol.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
