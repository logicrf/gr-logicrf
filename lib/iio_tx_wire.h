/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_tx_wire.h
 * @brief Exclusive IIOD data connection with cancellable strict batch replies.
 * @details libiio verifies control context/scan; this class transports existing IQ16 only.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-19
 * @note Final reply confirms network submission, not DMA/SSI consumption; cancellation never resends uncertain tails.
 ******************************************************************************/
#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace gr::logicrf {
/** @brief One IIOD connection; run performs IO, other threads cancel only; mutex protects fd closure. */
class iio_tx_wire {
public:
    explicit iio_tx_wire(std::function<void()> check);
    ~iio_tx_wire();
    /** @brief Open small noncyclic DMA blocks using the verified context's device ID/scan mask. */
    void open(const std::string& uri, const std::string& device,
              std::size_t samples, const std::string& mask, std::uint64_t pacing_bytes_per_second);
    /** @brief One WRITEBUF covers multiple DMA blocks; return after both reply stages succeed. */
    void push(const void* bytes, std::size_t count);
    /** @brief Normal CLOSE requires a zero reply; exceptions close the socket in destruction for remote cleanup. */
    void close();
    /** @brief Concurrent cancel calls shutdown only, preventing premature fd reuse by another thread. */
    void cancel() noexcept;
private:
    struct impl;
    std::unique_ptr<impl> d;
};
} // namespace gr::logicrf
