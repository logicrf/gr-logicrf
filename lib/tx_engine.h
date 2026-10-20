/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file tx_engine.h
 * @brief Bounded TX input queues and finite-stream lifecycle.
 * @details Support mock, managed finite IIO submission and USB digital TX; hardware startup requires independent ownership.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#pragma once
#include <gnuradio/logicrf/device.h>

namespace gr::logicrf {
/** @brief Shared TX engine separately counts accepted input, simulated consumption, IIO submission and verified USB consumption. */
class tx_engine {
public:
    explicit tx_engine(const device_options& options);
    ~tx_engine();
    void attach(unsigned channel);
    void detach(unsigned channel) noexcept;
    void enter(unsigned channel);
    void leave(unsigned channel) noexcept;
    void shutdown() noexcept;
    std::ptrdiff_t write(unsigned channel, const std::complex<float>* input, std::size_t count);
    std::map<std::string, std::uint64_t> stats(unsigned channel) const;
    std::string error() const;
    bool ready() const;
    bool done(unsigned channel) const;
    std::uint64_t request_stop();
    void finish_input(unsigned channel);
    void invalidate(const std::string& reason);
    void configure(const device_options& options);
    device_options options() const;
    void renew_lease();
    void confirm_playing();
    /** @brief Owner callbacks occur at start/stop only; the data path checks C++ monotonic permission. */
    void set_session(std::function<device_options(unsigned)> prepare,
                     std::function<void(unsigned,const std::string&)> guard,
                     std::function<void()> finish);
    void set_runtime_unlock(std::function<std::shared_ptr<void>()> unlock);
private:
    struct impl;
    std::unique_ptr<impl> d;
};
} // namespace gr::logicrf
