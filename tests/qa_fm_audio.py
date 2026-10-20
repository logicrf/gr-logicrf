"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file qa_fm_audio.py
@brief Verify audio-free construction has no effects and missing services cause explicit failure without crashes.
@details Separate subprocess uses a nonexistent local PA endpoint; preserve user environment and never connect H102.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.1.0
@date 2026-10-17
"""
import os
import subprocess
import sys
import unittest


class AudioFailure(unittest.TestCase):
    """Scheduler work after failed start must exit safely without passing null PA handles to the service library."""
    def test_unavailable_server_is_reported_without_abort(self):
        environment = dict(os.environ, PULSE_SERVER='unix:/run/nonexistent-h102-audio-qa/socket')
        code = '''
from gnuradio import blocks,gr,logicrf
sink=logicrf.fm_audio_sink(500)
assert not sink.error()
graph=gr.top_block()
graph.connect(blocks.vector_source_f([0.]*4800,False),sink)
graph.run()
assert sink.error()=='FM_AUDIO_SERVICE_UNAVAILABLE',sink.error()
assert sink.snapshot()['submitted_samples']==0
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
# End of file: qa_fm_audio.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
