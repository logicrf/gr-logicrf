/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_rx.h
 * @brief Native single-RX IIO buffer lifecycle and cancellation interface.
 * @details Each instance exclusively owns context/buffer; IQ conversion stays C++; control calls occur at guard boundaries only.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note libiio 0.x LE s16/16 I/Q four-byte scans only.
 ******************************************************************************/
#pragma once
#include <complex>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>
namespace gr::logicrf {
/** @brief One IIO data channel; cancel supports other threads; run has one reader only. */
class iio_rx {
public:
    using sample_block = std::vector<std::complex<float>>;
    struct hooks {
        std::function<bool()> permitted, stopping;
        std::function<void(const std::string&)> guard, fail;
        std::function<void()> ready;
        std::function<void(sample_block)> deliver;
        /// @brief Track actual refill bytes including warmup/empty blocks in C++ diagnostics without Python data-path calls.
        std::function<void(std::size_t)> refill;
    };
    iio_rx(std::string uri, unsigned channel, std::size_t block_samples,
           std::uint64_t limit, hooks callbacks);
    ~iio_rx();
    /** @brief Begin exclusive capture; fail propagates exceptions to the whole Device without erasing evidence. */
    void run() noexcept;
    /** @brief Normal stop ends guards while buffers exist; never fabricate success on exception. */
    void finish_window();
    /** @brief Cancel blocking refill under the destruction mutex; never call from signal handlers. */
    void cancel() noexcept;
    /** @brief End normally after the current complete refill; preserve in-flight buffers and queued data. */
    void request_finish() noexcept;
private:
    struct impl;
    std::unique_ptr<impl> d;
};
} // namespace gr::logicrf
