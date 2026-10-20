/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file stream_probe.h
 * @brief Native IQ diagnostic Sink for actual scheduler consumption and RAMP16 window checks without disk writes.
 * @details Retain counts and at most 16 prefix samples to avoid large acceptance IQ files.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note RAMP16's 65536-period ambiguity cannot replace native overflow guards or RF/UTC qualification.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <map>
#include <string>
#include <vector>
namespace gr::logicrf {
/** @brief Bounded diagnostic Sink; snapshot may run concurrently with work. */
class stream_probe : virtual public gr::sync_block {
public:
    using sptr = std::shared_ptr<stream_probe>;
    static sptr make(bool ramp16 = false);
    /** @brief Consumed samples, nonfinite/range errors and modulo-RAMP faults; no inferred absolute loss count. */
    virtual std::map<std::string,std::uint64_t> snapshot() const = 0;
    virtual std::vector<gr_complex> prefix() const = 0;
    /** @brief Actually received metadata tags with GNU Radio item offsets. */
    virtual std::vector<gr::tag_t> tags() const = 0;
    /** @brief Latest work block I/Q means, RMS, peak magnitude and clipping fraction without sampling/decimating capture. */
    virtual std::map<std::string,double> signal_snapshot() const = 0;
    /** @brief Latest original configuration/frame tags retain PMT uint64 integers without inferring UTC accuracy. */
    virtual pmt::pmt_t metadata() const = 0;
};
} // namespace gr::logicrf
