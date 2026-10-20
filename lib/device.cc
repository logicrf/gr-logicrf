/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file device.cc
 * @brief Shared bounded queues, dual IIO reader coordination, mock conversion and strict failure cleanup.
 * @details Whole-frame bounded queues/condition variables support partial reads without losing frame tails to scheduler sizing.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note Reject unsupported backends before start; never switch mock to hardware automatically.
 ******************************************************************************/
#include <gnuradio/logicrf/device.h>
#include "iio_rx.h"
#include "usb_rx.h"
#include "tx_engine.h"
#include <algorithm>
#include <atomic>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace gr::logicrf {
struct device::impl {
    /** @brief Per-channel queue entry; offset lets multiple GNU work calls consume the same frame. */
    struct frame {
        std::vector<std::complex<float>> samples;
        std::uint64_t first = 0;
        std::size_t offset = 0;
        usb_frame_metadata metadata{};
    };
    explicit impl(const device_options& opts) : opts(opts) {
        if (opts.direction=="tx") tx=std::make_unique<tx_engine>(opts);
    }
    std::unique_ptr<tx_engine> tx;
    device_options opts;
    mutable std::mutex mutex;
    std::mutex lifecycle, registry;
    std::condition_variable changed;
    std::array<std::deque<frame>, 2> queues;
    unsigned attached = 0, active = 0;
    bool stopping = true, finished = true, finishing = false;
    std::string failure;
    std::uint64_t generated = 0;
    std::thread worker;
    std::array<std::shared_ptr<iio_rx>, 2> readers;
    std::shared_ptr<usb_rx> usb_reader;
    std::array<std::uint64_t, 2> received = {};
    std::array<std::map<std::string, std::uint64_t>, 2> queue_metrics;
    unsigned ready_mask = 0, consumer_mask = 0;
    std::atomic<std::int64_t> lease_ns{0};
    bool owner_active = false;
    std::function<device_options(unsigned)> prepare;
    std::function<void(unsigned, const std::string&)> guard;
    std::function<void()> finish;
    std::function<std::shared_ptr<void>()> unlock_runtime;

    static std::int64_t now_ns()
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    bool permitted()
    {
        std::lock_guard<std::mutex> lock(mutex);
        return failure.empty() && lease_ns.load() > now_ns();
    }
    std::array<std::shared_ptr<iio_rx>,2> reader_snapshot()
    {
        std::lock_guard<std::mutex> lock(registry);
        return readers;
    }
    /** @brief Cancel without join, callable from data reader or permission monitor. */
    void fail(const std::string& reason)
    {
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (failure.empty()) failure = reason;
            stopping = true; changed.notify_all();
        }
        for (auto& reader : reader_snapshot()) if (reader) reader->cancel();
        std::shared_ptr<usb_rx> usb;{std::lock_guard<std::mutex> lock(registry);usb=usb_reader;}
        if(usb) usb->cancel();
    }
    /** @brief Independent contexts with coordinated errors, completion and cancellation for both channels. */
    void run_iio(unsigned channels)
    {
        std::vector<std::thread> threads;
        try {
            for (unsigned ch = 0; ch < 2; ++ch) {
                if (!(channels & (1U << ch))) continue;
                iio_rx::hooks hooks;
                hooks.permitted = [this] { return permitted(); };
                hooks.stopping = [this] { std::lock_guard<std::mutex> lock(mutex); return stopping; };
                hooks.guard = [this,ch](const std::string& stage) { guard(ch+1,stage); };
                hooks.fail = [this,ch](const std::string& error) { fail("RX"+std::to_string(ch+1)+":"+error); };
                hooks.ready = [this,ch] { std::lock_guard<std::mutex> lock(mutex); ready_mask |= 1U << ch; };
                hooks.refill = [this,ch](std::size_t bytes) {
                    std::lock_guard<std::mutex> lock(mutex);
                    auto& metrics = queue_metrics[ch];
                    ++metrics["refills"]; metrics["refill_bytes"] += bytes;
                    if (!bytes) ++metrics["empty_refills"];
                    else {
                        if (bytes < opts.frame_samples*4) ++metrics["short_refills"];
                        auto& smallest = metrics["min_nonzero_refill_bytes"];
                        if (!smallest || bytes < smallest) smallest = bytes;
                    }
                };
                hooks.deliver = [this,ch](iio_rx::sample_block samples) {
                    std::unique_lock<std::mutex> lock(mutex);
                    if (stopping) return;
                    // refill can burst accumulated kernel blocks. Bounded backpressure retains every sample;
                    // persistent consumer overload fails within 200 ms, with the ADC guard independently active.
                    if (queues[ch].size() >= opts.queue_frames) {
                        const auto started = now_ns();
                        auto& metrics = queue_metrics[ch];
                        ++metrics["wait_count"];
                        const bool available = changed.wait_for(lock, std::chrono::milliseconds(200), [this,ch] {
                            return stopping || queues[ch].size() < opts.queue_frames;
                        });
                        const auto elapsed = static_cast<std::uint64_t>(now_ns()-started);
                        metrics["wait_ns"] += elapsed;
                        metrics["max_wait_ns"] = std::max(metrics["max_wait_ns"],elapsed);
                        if (!available) throw std::runtime_error("RX_QUEUE_OVERFLOW_200MS");
                    }
                    if (stopping) return;
                    frame value; value.first = received[ch]; value.samples = std::move(samples);
                    received[ch] += value.samples.size();
                    generated = std::max(generated,received[ch]);
                    queues[ch].push_back(std::move(value));
                    auto& peak = queue_metrics[ch]["peak_frames"];
                    peak = std::max(peak,static_cast<std::uint64_t>(queues[ch].size()));
                    changed.notify_all();
                };
                auto reader = std::make_shared<iio_rx>(opts.uri,ch+1,opts.frame_samples,opts.sample_limit,std::move(hooks));
                {
                    std::lock_guard<std::mutex> lock(registry); readers[ch] = reader;
                }
                threads.emplace_back([reader] { reader->run(); });
            }
        } catch (const std::exception& error) { fail(error.what()); }
        for (auto& thread : threads) if (thread.joinable()) thread.join();
        std::lock_guard<std::mutex> lock(mutex);
        finished = true; changed.notify_all();
    }

    /** @brief One USB transport process serves whole-frame queues; no Python in the data path. */
    void run_usb(unsigned channels)
    {
        try {
            // Confirm all Sources entered work before RUN, outside the scheduler startup barrier.
            // This check does not extend queue backpressure deadlines; failed startup produces no IQ to discard.
            {
                std::unique_lock<std::mutex> lock(mutex);
                if(!changed.wait_for(lock,std::chrono::seconds(5),[this,channels]{
                    return stopping || consumer_mask==channels;
                }) || stopping) throw std::runtime_error("USB_CONSUMER_START_DEADLINE");
            }
            usb_rx::hooks hooks;
            hooks.permitted=[this]{return permitted();};
            hooks.stopping=[this]{std::lock_guard<std::mutex> lock(mutex);return stopping;};
            hooks.guard=[this](const std::string& stage){guard(0,stage);};
            hooks.fail=[this](const std::string& error){fail(error);};
            hooks.ready=[this,channels]{std::lock_guard<std::mutex> lock(mutex);ready_mask=channels;};
            hooks.partial=[this,channels](std::uint64_t bytes){std::lock_guard<std::mutex> lock(mutex);for(unsigned ch=0;ch<2;++ch) if(channels&(1U<<ch)) queue_metrics[ch]["usb_partial_tail_bytes"]=bytes;};
            hooks.deliver=[this](unsigned channel,std::vector<std::complex<float>> samples,const usb_frame_metadata& metadata){
                const unsigned ch=channel-1;
                std::unique_lock<std::mutex> lock(mutex);
                if(!failure.empty()) throw std::runtime_error(failure);
                ++queue_metrics[ch]["usb_wire_verified_frames"];
                if(stopping) {
                    queue_metrics[ch]["usb_cancel_tail_samples"]+=samples.size();
                    return;
                }
                // USB cannot hide overload with long reads; wait at most 20 ms for scheduler bursts, then stop if still full.
                if(queues[ch].size()>=opts.queue_frames) {
                    const auto start=now_ns();auto& m=queue_metrics[ch];++m["wait_count"];
                    const bool available=changed.wait_for(lock,std::chrono::milliseconds(20),[this,ch]{
                        return stopping || !failure.empty() || queues[ch].size()<opts.queue_frames;
                    });
                    const auto elapsed=static_cast<std::uint64_t>(now_ns()-start);m["wait_ns"]+=elapsed;
                    m["max_wait_ns"]=std::max(m["max_wait_ns"],elapsed);
                    if(!available || !failure.empty()) throw std::runtime_error("USB_RX_QUEUE_OVERFLOW_20MS");
                    if(stopping) {queue_metrics[ch]["usb_cancel_tail_samples"]+=samples.size();return;}
                }
                frame value;value.first=metadata.first;value.metadata=metadata;value.samples=std::move(samples);
                received[ch]+=value.samples.size();generated=std::max(generated,received[ch]);
                queues[ch].push_back(std::move(value));auto& m=queue_metrics[ch];++m["usb_frames"];
                m["peak_frames"]=std::max(m["peak_frames"],static_cast<std::uint64_t>(queues[ch].size()));
                changed.notify_all();
            };
            auto reader=std::make_shared<usb_rx>(opts,channels,std::move(hooks));
            {std::lock_guard<std::mutex> lock(registry);usb_reader=reader;}
            reader->run();
        } catch(const std::exception& error) {fail(error.what());}
        std::lock_guard<std::mutex> lock(mutex);finished=true;changed.notify_all();
    }

    /** @brief Generate a frame outside the shared lock and distribute atomically inside; overflow stops all channels. */
    void run(unsigned channels)
    {
        try {
            auto deadline = std::chrono::steady_clock::now();
            std::uint64_t index = 0;
            while (true) {
                std::size_t count = opts.frame_samples;
                if (opts.mock_samples) {
                    if (index >= opts.mock_samples) break;
                    count = std::min<std::uint64_t>(count, opts.mock_samples - index);
                }
                std::array<frame, 2> frames;
                for (unsigned ch = 0; ch != 2; ++ch) {
                    if (!(channels & (1U << ch))) continue;
                    auto& frame = frames[ch];
                    frame.first = index;
                    frame.samples.resize(count);
                    for (std::size_t n = 0; n != count; ++n) {
                        auto code = static_cast<int>((index + n + ch * 97U) & 65535U);
                        frame.samples[n] = {float(code - 32768) / 32768.0f,
                                            float(32767 - code) / 32768.0f};
                    }
                }
                std::unique_lock<std::mutex> lock(mutex);
                if (stopping || finishing) break;
                for (unsigned ch = 0; ch != 2; ++ch) {
                    if ((channels & (1U << ch)) && queues[ch].size() >= opts.queue_frames) {
                        failure = "RX_QUEUE_OVERFLOW channel=" + std::to_string(ch + 1);
                        stopping = true;
                    }
                }
                if (stopping) break;
                for (unsigned ch = 0; ch != 2; ++ch)
                    if (channels & (1U << ch)) {queues[ch].push_back(std::move(frames[ch]));received[ch]+=count;}
                generated = index += count;
                changed.notify_all();
                deadline += std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                    std::chrono::duration<double>(double(count) / opts.sample_rate));
                if (changed.wait_until(lock, deadline, [this] { return stopping || finishing; })) break;
            }
        } catch (const std::exception& ex) {
            std::lock_guard<std::mutex> lock(mutex);
            failure = ex.what();
        }
        std::lock_guard<std::mutex> lock(mutex);
        finished = true;
        changed.notify_all();
    }

    /** @brief Perform blocking cleanup outside the data lock; end normal guards before cancellation/buffer destruction. */
    void stop() noexcept
    {
        auto release_runtime = unlock_runtime ? unlock_runtime() : nullptr;
        {
            std::lock_guard<std::mutex> lock(mutex);
            stopping = true; changed.notify_all();
        }
        std::shared_ptr<usb_rx> usb;{std::lock_guard<std::mutex> lock(registry);usb=usb_reader;}
        if(usb) {
            try {usb->finish_window();}
            catch(const std::exception& error) {fail(std::string("USB_STOP:")+error.what());}
        }
        for (auto& reader : reader_snapshot()) {
            if (!reader) continue;
            try { reader->finish_window(); }
            catch (const std::exception& error) { fail(std::string("GUARD_END:")+error.what()); }
        }
        for (auto& reader : reader_snapshot()) if (reader) reader->cancel();
        if (worker.joinable()) worker.join();
        if(usb) {
            std::lock_guard<std::mutex> lock(mutex);
            for(unsigned ch=0;ch<2;++ch) {
                std::uint64_t remaining=0;for(const auto& f:queues[ch]) remaining+=f.samples.size()-f.offset;
                queue_metrics[ch]["usb_queued_at_stop_samples"]=remaining;
            }
        }
        if (owner_active) {
            try { finish(); }
            catch (const std::exception& error) {
                std::lock_guard<std::mutex> lock(mutex);
                // Queues/readers are stopped; retain the first error and append recovery failures rather than displaying cancellation alone.
                if(!failure.empty()) failure+=";";
                failure+=std::string("OWNER_POST:")+error.what();
            }
            owner_active = false;
        }
        std::lock_guard<std::mutex> lock(registry);
        readers = {}; usb_reader.reset();
    }
};

static unsigned bit(unsigned channel)
{
    if (channel < 1 || channel > 2) throw std::invalid_argument("channel must be 1 or 2");
    return 1U << (channel - 1);
}

device::sptr device::make(const device_options& options)
{
    if (!std::isfinite(options.sample_rate) || options.sample_rate <= 0 ||
        options.sample_rate > 61440000 || !std::isfinite(options.center_frequency) ||
        options.center_frequency < 50000000 || options.center_frequency > 5800000000.0)
        throw std::invalid_argument("invalid H102 sample rate/frequency");
    if (!options.frame_samples || options.frame_samples > 1048576 ||
        !options.queue_frames || options.queue_frames > 1024)
        throw std::invalid_argument("invalid queue/frame size");
    if (options.backend != "mock" && options.backend != "iio" && options.backend != "usb")
        throw std::invalid_argument("unknown backend");
    if (options.direction!="rx" && options.direction!="tx")
        throw std::invalid_argument("direction must be rx or tx");
    if (options.direction=="tx" && (options.queue_frames<2 ||
        options.frame_samples*options.queue_frames>4194304))
        throw std::invalid_argument("invalid TX length/queue: limit is 16MiB per channel");
    return sptr(new device(options));
}
device::device(const device_options& options) : d(std::make_unique<impl>(options)) {}
device::~device() { shutdown(); }
device_options device::options() const { if(d->tx) return d->tx->options(); std::lock_guard<std::mutex> lock(d->mutex); return d->opts; }
std::string device::error() const { if(d->tx) return d->tx->error(); std::lock_guard<std::mutex> lock(d->mutex); return d->failure; }
std::uint64_t device::produced() const { std::lock_guard<std::mutex> lock(d->mutex); return d->generated; }
void device::attach(unsigned channel)
{
    if (d->tx) {d->tx->attach(channel);return;}
    const auto mask = bit(channel);
    std::lock_guard<std::mutex> lock(d->mutex);
    if (d->active || (d->attached & mask)) throw std::runtime_error("RX_CHANNEL_BUSY");
    d->attached |= mask;
}
void device::detach(unsigned channel) noexcept
{
    if (d->tx) {d->tx->detach(channel);return;}
    leave(channel);
    std::lock_guard<std::mutex> lock(d->mutex);
    if (channel >= 1 && channel <= 2) d->attached &= ~(1U << (channel - 1));
}
void device::enter(unsigned channel)
{
    if (d->tx) {d->tx->enter(channel);return;}
    const auto mask = bit(channel);
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    unsigned channels = 0;
    {
        std::lock_guard<std::mutex> lock(d->mutex);
        if (!(d->attached & mask) || (d->active & mask)) throw std::runtime_error("RX_CHANNEL_STATE");
        unsigned count = ((d->attached & 1) ? 1 : 0) + ((d->attached & 2) ? 1 : 0);
        if (d->opts.backend == "iio" && d->opts.sample_rate * 4 * count > 64000000.0)
            throw std::runtime_error("IIO_RX_BUDGET_EXCEEDED");

        if(d->opts.backend=="usb" && d->opts.sample_rate*4*count>320000000.0)
            throw std::runtime_error("USB_RX_BUDGET_EXCEEDED");
        if (d->opts.backend != "mock" && (!d->prepare || !d->guard || !d->finish))
            throw std::runtime_error("MANAGED_SESSION_REQUIRED");
        if (!d->active) {
            if (d->worker.joinable()) throw std::runtime_error("PREVIOUS_WORKER_NOT_JOINED");
            // GNU starts blocks on scheduler threads; the first channel must await aggregation rather than return early EOF.
            d->stopping = d->finished = false;
            d->finishing = false;
            d->failure.clear(); d->generated = 0; d->received = {}; d->queue_metrics = {}; d->ready_mask = 0;d->consumer_mask=0;
            for (auto& queue : d->queues) queue.clear();
        }
        d->active |= mask;
        if (d->active != d->attached) return;
        channels = d->attached;
    }
    try {
        if (d->opts.backend != "mock") {
            auto expected = d->opts;
            d->owner_active = true;
            auto actual = d->prepare(channels);
            if (actual.backend != expected.backend || actual.uri != expected.uri ||
                actual.sample_rate != expected.sample_rate || actual.boot_id.empty() || actual.metadata_json.empty())
                throw std::runtime_error("IIO_PREPARED_IDENTITY_RATE_MISMATCH");
            {
                std::lock_guard<std::mutex> lock(d->mutex); d->opts = actual;
            }
            if (!d->permitted()) throw std::runtime_error("IIO_PREPARED_PERMISSION_MISSING");
            if(actual.backend=="usb" && (actual.usb_socket.empty() || actual.usb_configuration.empty() ||
               !actual.usb_stream_id || !actual.usb_time_epoch)) throw std::runtime_error("USB_PREPARED_INPUT_MISSING");
            d->worker = std::thread([this,channels] {
                if(d->opts.backend=="usb") d->run_usb(channels); else d->run_iio(channels);
            });
        } else {
            { std::lock_guard<std::mutex> lock(d->mutex); d->ready_mask = channels; }
            d->worker = std::thread([this,channels] { d->run(channels); });
        }
    } catch (...) {
        d->stop();
        std::lock_guard<std::mutex> lock(d->mutex);
        d->active = 0; d->stopping = d->finished = true;
        throw;
    }
}
void device::leave(unsigned channel) noexcept
{
    if (d->tx) {d->tx->leave(channel);return;}
    if (channel < 1 || channel > 2) return;
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    bool last = false;
    {
        std::lock_guard<std::mutex> lock(d->mutex);
        auto mask = 1U << (channel - 1);
        if (d->active & mask) { d->active &= ~mask; last = !d->active; }
    }
    if (last) d->stop();
}
void device::shutdown() noexcept
{
    if (d->tx) {d->tx->shutdown();return;}
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    { std::lock_guard<std::mutex> lock(d->mutex); d->active = 0; }
    d->stop();
}
void device::request_rx_stop()
{
    if(d->tx) throw std::runtime_error("TX_DEVICE_HAS_NO_RX");
    {
        std::lock_guard<std::mutex> lock(d->mutex);
        if(d->opts.backend=="usb") throw std::runtime_error("USB_RX_STOP_REQUIRES_MANAGED_BOARD_STOP");
        if(!d->active || d->stopping || d->ready_mask!=d->attached || !d->failure.empty())
            throw std::runtime_error("RX_STOP_REQUIRES_READY_STREAM");
        d->finishing=true;d->changed.notify_all();
    }
    for(auto& reader:d->reader_snapshot()) if(reader) reader->request_finish();
}
void device::set_session(std::function<device_options(unsigned)> prepare,
                         std::function<void(unsigned,const std::string&)> guard,
                         std::function<void()> finish)
{
    if (d->tx) {d->tx->set_session(std::move(prepare),std::move(guard),std::move(finish));return;}
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    if (d->active) throw std::runtime_error("STOP_REQUIRED_FOR_SESSION");
    d->prepare = std::move(prepare); d->guard = std::move(guard); d->finish = std::move(finish);
}
void device::set_runtime_unlock(std::function<std::shared_ptr<void>()> unlock)
{
    d->unlock_runtime = std::move(unlock);
    if(d->tx) d->tx->set_runtime_unlock(d->unlock_runtime);
}
void device::renew_lease() { if(d->tx) {d->tx->renew_lease();return;} d->lease_ns.store(impl::now_ns() + 2000000000LL); }
void device::invalidate(const std::string& reason) { if(d->tx) {d->tx->invalidate(reason);return;} d->lease_ns.store(0); d->fail(reason); }
bool device::ready() const
{
    if(d->tx) return d->tx->ready();
    std::lock_guard<std::mutex> lock(d->mutex);
    return d->active && d->ready_mask == d->attached && d->failure.empty();
}
std::uint64_t device::received(unsigned channel) const
{
    bit(channel); std::lock_guard<std::mutex> lock(d->mutex); return d->received[channel-1];
}
std::map<std::string,std::uint64_t> device::queue_stats(unsigned channel) const
{
    bit(channel); std::lock_guard<std::mutex> lock(d->mutex); return d->queue_metrics[channel-1];
}
void device::configure(const std::string& profile,double rate,double frequency)
{
    auto options = this->options(); options.sample_rate = rate; options.center_frequency = frequency;
    // Reuse construction validation without connecting; defer hardware transactions to prepare.
    auto checked = device::make(options);
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    std::lock_guard<std::mutex> lock(d->mutex);
    if (d->active) throw std::runtime_error("STOP_REQUIRED_FOR_CONFIGURE");
    if (d->tx) {options.configuration=profile;d->tx->configure(options);}
    d->opts.configuration = profile; d->opts.sample_rate = rate; d->opts.center_frequency = frequency;
}
std::ptrdiff_t device::read(unsigned channel, std::complex<float>* output,
                           std::size_t capacity, std::uint64_t& first_index, usb_frame_metadata* metadata, bool* frame_start)
{
    if(d->tx) throw std::runtime_error("TX_DEVICE_CANNOT_READ");
    bit(channel);
    if (!capacity) return 0;
    std::unique_lock<std::mutex> lock(d->mutex);
    ++d->queue_metrics[channel-1]["read_calls"];
    d->consumer_mask |= 1U<<(channel-1);d->changed.notify_all();
    auto& queue = d->queues[channel - 1];
    d->changed.wait_for(lock, std::chrono::milliseconds(25), [&] {
        return !queue.empty() || d->finished || d->stopping;
    });
    if (!d->failure.empty()) return -2;
    if (queue.empty()) return (d->finished || (d->stopping && d->opts.backend!="usb")) ? -1 : 0;
    auto& frame = queue.front();
    if(metadata) *metadata=frame.metadata;
    if(frame_start) *frame_start=frame.offset==0;
    auto count = std::min(capacity, frame.samples.size() - frame.offset);
    first_index = frame.first + frame.offset;
    std::copy_n(frame.samples.data() + frame.offset, count, output);
    frame.offset += count;
    d->queue_metrics[channel-1]["output_samples"]+=count;
    if (frame.offset == frame.samples.size()) {
        queue.pop_front(); d->changed.notify_all();
    }
    return static_cast<std::ptrdiff_t>(count);
}
void device::set_center_frequency(double value)
{
    if (!std::isfinite(value) || value < 50000000 || value > 5800000000.0)
        throw std::invalid_argument("frequency outside 50MHz..5.8GHz");
    std::lock_guard<std::mutex> lock(d->mutex);
    if (d->active) throw std::runtime_error("STOP_REQUIRED_FOR_TUNE");
    if(d->tx) {auto options=d->opts;options.center_frequency=value;d->tx->configure(options);}
    d->opts.center_frequency = value;
}
void device::set_frame_samples(std::size_t samples)
{
    auto options=this->options();options.frame_samples=samples;
    auto checked=device::make(options);
    std::lock_guard<std::mutex> lifecycle(d->lifecycle);
    std::lock_guard<std::mutex> lock(d->mutex);
    if(d->active) throw std::runtime_error("STOP_REQUIRED_FOR_BLOCK_SIZE");
    if(d->tx) d->tx->configure(options);
    d->opts.frame_samples=samples;
}
std::ptrdiff_t device::write(unsigned channel,const std::complex<float>* input,std::size_t count)
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_CANNOT_WRITE");
    return d->tx->write(channel,input,count);
}
bool device::tx_input_done(unsigned channel) const
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_HAS_NO_TX");
    return d->tx->done(channel);
}
std::uint64_t device::request_tx_stop()
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_HAS_NO_TX");
    return d->tx->request_stop();
}
void device::finish_tx_input(unsigned channel)
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_HAS_NO_TX");
    d->tx->finish_input(channel);
}
std::map<std::string,std::uint64_t> device::tx_stats(unsigned channel) const
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_HAS_NO_TX");
    return d->tx->stats(channel);
}
void device::confirm_tx_playing()
{
    if(!d->tx) throw std::runtime_error("RX_DEVICE_HAS_NO_TX");
    d->tx->confirm_playing();
}
} // namespace gr::logicrf
