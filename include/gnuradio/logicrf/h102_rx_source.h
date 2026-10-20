/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_rx_source.h
 * @brief H102 GNU Radio Source with one complex output per channel.
 * @details Sources share a Device; independent DMA channels do not imply coherent synchronization.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-16
 * @note Construction never starts data; errors use events and Device.error.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <gnuradio/logicrf/device.h>

namespace gr::logicrf {
/** @brief Shared-session single-channel RX delivering normalized cf32 without fabricated UTC. */
class h102_rx_source : virtual public gr::sync_block {
public:
    using sptr = std::shared_ptr<h102_rx_source>;
    /** @brief Register unique RX1/RX2; throw on duplicates or active-session registration. */
    static sptr make(device::sptr board, unsigned channel);
};
} // namespace gr::logicrf
