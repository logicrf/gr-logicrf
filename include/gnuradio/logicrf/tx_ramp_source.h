/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file tx_ramp_source.h
 * @brief GNU Radio digital TX test source bound to the actual owner's seed.
 * @details Generate finite cf32 RAMP32 only; Sink quantizes/encodes/transports without direct USB access here.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.5.0
 * @date 2026-10-16
 * @note Not an RF waveform source or unlimited-duration qualification.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <gnuradio/logicrf/device.h>
namespace gr::logicrf {
/** @brief Read actual seed on first work after aggregated start, never guess session settings during construction. */
class tx_ramp_source : virtual public gr::sync_block {
public:
    using sptr=std::shared_ptr<tx_ramp_source>;
    static sptr make(device::sptr board,unsigned channel);
};
}
