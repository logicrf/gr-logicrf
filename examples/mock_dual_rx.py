#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file mock_dual_rx.py
@brief Run dual RX mocks without hardware and check flowgraph results.
@details Share one Device for finite capture; do not simulate UTC or RF qualification; return nonzero on error.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-16
"""
from gnuradio import gr, blocks, logicrf


def main():
    """Run the real GNU scheduler and check exact finite counts/errors for both channels."""
    board = logicrf.make_device(mock_samples=12345, sample_rate=100000)
    graph = gr.top_block()
    sinks = []
    for channel in (1, 2):
        source = logicrf.h102_rx_source(board, channel)
        sink = blocks.vector_sink_c()
        graph.connect(source, sink)
        sinks.append(sink)
    graph.run()
    if board.error() or any(len(s.data()) != 12345 for s in sinks):
        raise RuntimeError('mock capture failed: ' + board.error())
    print('PASS_MOCK_DUAL_RX', [len(s.data()) for s in sinks])


if __name__ == '__main__':
    main()

# End of file: mock_dual_rx.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
