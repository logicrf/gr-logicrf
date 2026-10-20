"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file api_pipe.py
@brief Bounded API line IO for the existing USB controller.
@details Transfer control JSON only, never IQ, fabricated heartbeats or remote commands.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import time


class Pipe:
    """One reader owns a local line cache; only the controller owns channel lifetime."""
    def __init__(self,channel,kind):self.channel,self.kind,self.pending=channel,kind,bytearray()
    def write(self,value):self.channel.sendall(value)
    def flush(self):pass
    def close(self):
        if self.kind=='stdin':self.channel.close()
    def readline(self,limit=65537):
        """Return at most one bounded line; EOF returns remainder; native permission detects control loss."""
        while b'\n' not in self.pending and len(self.pending)<limit:
            if self.channel.recv_ready():self.pending.extend(self.channel.recv(min(65536,limit-len(self.pending))))
            elif self.channel.exit_status_ready():break
            else:time.sleep(.003)
        end=self.pending.find(b'\n')+1
        end=min(end or len(self.pending),limit)
        value=bytes(self.pending[:end]);del self.pending[:end]
        return value
    def read(self):
        """Collect bounded stderr from exited control only; never wait for unlimited data."""
        return self.channel.recv_stderr(65536)

# End of file: api_pipe.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
