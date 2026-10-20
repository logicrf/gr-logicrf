/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file device.h
 * @brief Shared H102 device sessions, channel queues and stop/error boundaries.
 * @details Construction is hardware-free; register sources before coordinated start; strict overflow never drops old frames.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note Mocks do not qualify IIO/USB/RF. All sample counts are per channel.
 ******************************************************************************/
#pragma once
#include <complex>
#include <gnuradio/logicrf/rx_frame_info.h>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <map>
#include <string>

namespace gr::logicrf {
/** @brief Static Device parameters: sample rate/center frequency in Hz, channel number 1 or 2. */
struct device_options {
    std::string direction = "rx";      ///< rx or tx; shared sessions aggregate two unidirectional native engines.
    std::uint64_t tx_samples = 65536;   ///< Finite per-channel length; 0 runs continuously and drains after explicit EOF/stop.
    std::string tx_validation = "none"; ///< none accepts arbitrary IQ; ramp32 is for digital protocol checks only.
    std::string usb_tx_templates;       ///< Frozen 256-byte TX template supplied by the owner.
    std::uint32_t usb_tx_seed = 0;      ///< RAMP32 seed at actual owner consumption, not a credential.
    std::string backend = "mock";       ///< Explicit mock/iio/usb selection; hardware requires managed ownership.
    std::string uri;                    ///< Board control ip:address; USB local data socket is separate.
    std::string configuration = "mock"; ///< Exact configuration identity, never inferred from bandwidth alone.
    double sample_rate = 1000000;
    double center_frequency = 150000000;
    std::size_t frame_samples = 4096;
    std::size_t queue_frames = 64;
    std::uint64_t sample_limit = 0;     ///< Finite IIO output samples per channel; 0 is continuous.
    double rf_bandwidth = 0;           ///< Analog bandwidth in Hz from actual prepare readback.
    std::string boot_id, metadata_json; ///< Actual identity/active_info without credentials.
    std::string usb_socket, usb_configuration; ///< Private session socket and original 112-byte board readback.
    std::uint64_t usb_stream_id = 0;
    std::uint32_t usb_time_epoch = 0;
    std::uint64_t mock_samples = 0;     ///< 0 is continuous; otherwise exact finite per-channel length.
};

/** @brief Shared device lifecycle; locks protect queues and the IQ path never calls Python.
 * @details One native Device owns two same-direction Sources/Sinks; reject duplicate channels; configure while stopped.
 *          Final Source exit cancels waits and joins workers; destruction performs the same cleanup.
 */
class device {
public:
    using sptr = std::shared_ptr<device>;
    /** @brief Validate static input and construct without networking, USB or RF attribute writes. */
    static sptr make(const device_options& options);
    ~device();
    device(const device&) = delete;
    device& operator=(const device&) = delete;
    /** @brief Return a snapshot of construction parameters. */
    device_options options() const;
    /** @brief Register/unregister a unique channel; registration requires stopped state. */
    void attach(unsigned channel);
    void detach(unsigned channel) noexcept;
    /** @brief Start the shared coordinator/backend reader only after every registered Source starts. */
    void enter(unsigned channel);
    void leave(unsigned channel) noexcept;
    /** @brief Read up to capacity cf32 samples; 0 waits, -1 ends normally, -2 reports strict failure. */
    std::ptrdiff_t read(unsigned channel, std::complex<float>* output,
                        std::size_t capacity, std::uint64_t& first_index,
                        usb_frame_metadata* metadata = nullptr, bool* frame_start = nullptr);
    /** @brief Retain errors until next start; an empty string means no recorded failure. */
    std::string error() const;
    /** @brief Change tuning metadata while stopped; reject active changes; never write hardware. */
    void set_center_frequency(double value);
    /** @brief Bind managed control before start; call finish for cleanup even when prepare fails. */
    void set_session(std::function<device_options(unsigned)> prepare,
                     std::function<void(unsigned, const std::string&)> guard,
                     std::function<void()> finish);
    /** @brief Release the interpreter lock during blocking cleanup; not a device permission interface. */
    void set_runtime_unlock(std::function<std::shared_ptr<void>()> unlock);
    /** @brief Native health monitor renews a two-second local permit every 0.5 s without Python in the IQ path. */
    void renew_lease();
    /** @brief Revoke permission and cancel all refills; retain errors without a blocking join. */
    void invalidate(const std::string& reason);
    /** @brief End all Sources/native ownership explicitly; record post failures in error. */
    void shutdown() noexcept;
    /** @brief Seal IIO/mock after the next complete capture block, deliver queues, then let the application graph.wait. */
    void request_rx_stop();
    /** @brief All registered IIO channels completed warmup/guard; mock is ready after start. */
    bool ready() const;
    /** @brief Actual enqueued samples per channel, not downstream consumption or absolute ADC indices. */
    std::uint64_t received(unsigned channel) const;
    /** @brief IIO queue high-water mark and bounded backpressure count/total/max wait, with times in ns. */
    std::map<std::string, std::uint64_t> queue_stats(unsigned channel) const;
    /** @brief Set the next profile plan while stopped; hardware changes occur at next start. */
    void configure(const std::string& profile, double rate, double frequency);
    /** @brief Set IIO block length while stopped; use smaller low-rate blocks to preserve refill timeout margin. */
    void set_frame_samples(std::size_t samples);
    /** @brief Produced samples per channel, not proof of hardware consumption. */
    std::uint64_t produced() const;
    /** @brief Accepted TX input: -1 finite completion/stop, -2 strict failure, 0 backpressure. */
    std::ptrdiff_t write(unsigned channel, const std::complex<float>* input, std::size_t count);
    /** @brief Whether all finite TX input was accepted, not whether hardware consumed it. */
    bool tx_input_done(unsigned channel) const;
    /** @brief Request continuous-TX drain after a future complete transfer boundary; return shared per-channel target. */
    std::uint64_t request_tx_stop();
    /** @brief Seal this channel after actual upstream EOF; forced external stop is never treated as EOF. */
    void finish_tx_input(unsigned channel);
    /** @brief TX input, IIO submission, USB verification, simulation and cancellation counters; no RF inference. */
    std::map<std::string, std::uint64_t> tx_stats(unsigned channel) const;
    /** @brief Controller confirms readiness after checking PLAY and host S; record state without operating hardware. */
    void confirm_tx_playing();
private:
    explicit device(const device_options& options);
    struct impl;
    std::unique_ptr<impl> d;
};
} // namespace gr::logicrf
