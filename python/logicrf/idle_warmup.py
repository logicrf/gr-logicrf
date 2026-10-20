"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file idle_warmup.py
@brief Warm fixed API channels during the first read-only check without starting hardware ownership.
@details Call bounded warm only; the caller explicitly claims/starts after full identity/profile checks.
         On check failure/cancel, join warmup and close the idle channel without background reconnection.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-18
"""
from concurrent.futures import ThreadPoolExecutor


class IdleWarmup:
    """Warmup owned by one preparation thread, without sending user plans/start commands to a background task.

    link.warm retains fixed API connect/reply/warmup deadlines. The caller must close in finally;
    claim transfers responsibility to normal ownership lifecycle; close never interrupts a claimed session.
    """
    def __init__(self, link):
        self.link = link
        self.claimed = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='h102-idle-warm')
        self.pending = self.pool.submit(link.warm)

    def claim(self, info, request):
        """Freeze requests after warmup and caller identity/profile checks, still without starting hardware.

        Propagate warmup errors unchanged. The caller must next use normal start/guard/post.
        """
        self.pending.result()
        self.link.info = info
        self.link.expected = lambda: request
        self.claimed = True
        return self.link

    def close(self):
        """Join the worker; close unclaimed channels even after warmup failure without swallowing close errors."""
        self.pool.shutdown(wait=True)
        if not self.claimed:
            if self.link.active:
                raise RuntimeError('IDLE_WARMUP_UNEXPECTED_ACTIVE_OWNER')
            self.link.close(stop=False)


# End of file: idle_warmup.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
