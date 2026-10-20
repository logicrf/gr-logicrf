/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_tx_sink.h
 * @brief H102 TX Sink with one complex input per channel.
 * @details C++ handles the data path; finite length comes from Device.tx_samples.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 * @note Managed IIO checks finite submissions; USB digital TX checks SSI; neither qualifies RF transmission.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <gnuradio/logicrf/device.h>

namespace gr::logicrf {
/** @brief One Sink per TX1/2; explicit TX Device shares queue lifecycle. */
class h102_tx_sink : virtual public gr::sync_block {
public:
    using sptr = std::shared_ptr<h102_tx_sink>;
    /** @brief Register one unique channel without networking, USB or RF changes. */
    static sptr make(device::sptr board, unsigned channel);
};
} // namespace gr::logicrf
