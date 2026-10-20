/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_replay_tagger.h
 * @brief Native H1MI integer metadata restoration for cf32 replay streams.
 * @details No hardware connection; read H1MI records in order with bounded memory and file-sample offsets.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-17
 * @note Background ReplayPlan checks whole-file SHA; valid digital time does not imply RF UTC calibration.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <string>
namespace gr::logicrf {
/** @brief Pass cf32 and restore tags natively; serialize configure with start/stop. */
class h102_replay_tagger : virtual public gr::sync_block {
public:
    using sptr=std::shared_ptr<h102_replay_tagger>;
    static sptr make(unsigned channel);
    /** @brief Set verified index/original recording identity while stopped; backend is iio/usb/mock only. */
    virtual void configure(const std::string& index,const std::string& backend,
                           double rate,double frequency,const std::string& configuration,
                           const std::string& boot_id,const std::string& active_info_json)=0;
    /** @brief Empty error means no detected index fault; also check EOF, counts and SHA. */
    virtual std::string error() const=0;
};
} // namespace gr::logicrf
