/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_metadata_sink.h
 * @brief Write verified USB frame tags to separate 128-byte index records without copying IQ.
 * @details Each entry stores cf32 sample offset and native integer time/identity; IIO has no native frame tags.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.7.0
 * @date 2026-10-16
 ******************************************************************************/
#pragma once
#include <gnuradio/logicrf/device.h>
#include <gnuradio/sync_block.h>
namespace gr::logicrf {
/** @brief Observe RX only without session ownership; file failures also revoke shared Device permission. */
class h102_metadata_sink : virtual public gr::sync_block {
public:
    using sptr=std::shared_ptr<h102_metadata_sink>;
    static sptr make(device::sptr board,const std::string& path,unsigned channel);
    /** @brief Completed index entries; normal file size is exactly records*128. */
    virtual std::uint64_t records() const=0;
    /** @brief open/write/fsync/close errors; empty means no file error. */
    virtual std::string error() const=0;
    /** @brief Change the next exclusively created file path while stopped; do not open it yet. */
    virtual void set_path(const std::string& path)=0;
};
} // namespace gr::logicrf
