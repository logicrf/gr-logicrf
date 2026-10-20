/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_tx.h
 * @brief Single-channel noncyclic IIO submission and bounded cancellation interface.
 * @details Callers provide actual IQ16 blocks; successful submission does not imply SSI or RF consumption.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.7.0
 * @date 2026-10-19
 * @note libiio 0.x LE s16/16 dual-output scans only; one writer per instance.
 ******************************************************************************/
#pragma once
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>
namespace gr::logicrf {
/** @brief Own context/data connection exclusively; cancel across threads, destroy during run cleanup only. */
class iio_tx {
public:
    struct hooks {
        std::function<bool()> permitted, stopping;
        std::function<bool(std::vector<std::uint32_t>&)> take;
        /** @brief Retrieve existing blocks only; never wait for future input or predeclare unknown IQ. */
        std::function<bool(std::vector<std::uint32_t>&)> take_available;
        std::function<void(std::size_t)> attempting;
        std::function<void(const std::vector<std::uint32_t>&)> submitted;
        std::function<void(const std::string&)> guard, fail;
        std::function<void()> ready;
    };
    iio_tx(std::string uri, unsigned channel, std::size_t block_samples,
           double sample_rate, hooks callbacks);
    ~iio_tx();
    /** @brief Submit finite queues continuously with actual tail lengths; never resend uncertain failed blocks. */
    void run() noexcept;
    /** @brief Cancel blocking network push, serialized with buffer destruction. */
    void cancel() noexcept;
private:
    struct impl;
    std::unique_ptr<impl> d;
};
} // namespace gr::logicrf
