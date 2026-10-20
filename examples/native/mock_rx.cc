/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file mock_rx.cc
 * @brief Create an installed C++ receive flowgraph and verify complete sample delivery.
 * @details Mock only, without board access; hardware backends also require managed set_session callbacks.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.7.0
 * @date 2026-10-16
 ******************************************************************************/
#include <gnuradio/logicrf/device.h>
#include <gnuradio/logicrf/h102_rx_source.h>
#include <gnuradio/blocks/null_sink.h>
#include <gnuradio/top_block.h>
#include <complex>
#include <iostream>

/** @brief Run the real GNU scheduler; fail mismatched source/downstream counts or errors. */
int main()
{
    gr::logicrf::device_options options;
    options.mock_samples = 12345;
    auto board = gr::logicrf::device::make(options);
    auto source = gr::logicrf::h102_rx_source::make(board, 1);
    auto sink = gr::blocks::null_sink::make(sizeof(std::complex<float>));
    auto graph = gr::make_top_block("logicrf-native-mock");
    graph->connect(source, 0, sink, 0);
    graph->run();
    board->shutdown();
    if (!board->error().empty() || board->received(1) != 12345 || sink->nitems_read(0) != 12345) {
        std::cerr << "FAIL " << board->error() << '\n';
        return 1;
    }
    std::cout << "PASS_NATIVE_INSTALLED samples=" << sink->nitems_read(0) << '\n';
    return 0;
}

// End of file: mock_rx.cc
// Contact: info@logicrf.com
// Website: https://www.logicrf.com
