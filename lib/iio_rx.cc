/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_rx.cc
 * @brief Strict scan validation, warmup guards, bounded cancellation and native IQ16 conversion.
 * @details End normal guards before buffer destruction; errors revoke permission/cancel without lossless qualification.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note Ordinary IIO has no native sample sequence; internal index counts host output only.
 ******************************************************************************/
#include "iio_rx.h"
#include <iio.h>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <thread>
#include <mutex>
#include <atomic>
#include <stdexcept>
#include <sstream>
#include <utility>

namespace gr::logicrf {
struct iio_rx::impl {
    std::string uri;
    unsigned channel;
    std::size_t samples;
    std::uint64_t limit;
    hooks callback;
    std::mutex io_mutex, window_mutex;
    iio_buffer* buffer = nullptr;
    bool armed = false, ended = false;
    std::atomic<bool> finish_requested{false};
};
iio_rx::iio_rx(std::string uri, unsigned channel, std::size_t samples,
               std::uint64_t limit, hooks callback) : d(std::make_unique<impl>())
{
    d->uri = std::move(uri); d->channel = channel; d->samples = samples;
    d->limit = limit; d->callback = std::move(callback);
}
iio_rx::~iio_rx() = default;
void iio_rx::request_finish() noexcept {d->finish_requested=true;}
void iio_rx::cancel() noexcept
{
    std::lock_guard<std::mutex> lock(d->io_mutex);
    if (d->buffer) iio_buffer_cancel(d->buffer);
}
void iio_rx::finish_window()
{
    std::lock_guard<std::mutex> lock(d->window_mutex);
    if (!d->armed || d->ended) return;
    if (d->callback.permitted()) d->callback.guard("end");
    d->ended = true;
}
void iio_rx::run() noexcept
{
    iio_context* context = nullptr;
    iio_device* device = nullptr;
    bool changed = false;
    try {
        if (d->callback.stopping()) return;
        unsigned major = 0, minor = 0;
        char git[8] = {};
        iio_library_get_version(&major, &minor, git);
        if (major != 0 || minor < 23) throw std::runtime_error("LIBIIO_ABI_NOT_0_23_PLUS");
        if (!d->callback.permitted()) throw std::runtime_error("OWNER_PERMISSION_BEFORE_CONTEXT");
        context = iio_create_context_from_uri(d->uri.c_str());
        if (!context) throw std::runtime_error("IIO_CONTEXT: " + std::to_string(errno));
        if (iio_context_set_timeout(context, 1000) < 0) throw std::runtime_error("IIO_TIMEOUT_SET");
        device = iio_context_find_device(context, d->channel == 1 ? "axi-adrv9002-rx-lpc" : "axi-adrv9002-rx2-lpc");
        if (!device) throw std::runtime_error("IIO_RX_DEVICE");
        for (unsigned n = 0; n < iio_device_get_channels_count(device); ++n)
            iio_channel_disable(iio_device_get_channel(device, n));
        iio_channel* scan[2] = {iio_device_find_channel(device,"voltage0_i",false),
                                iio_device_find_channel(device,"voltage0_q",false)};
        for (int n = 0; n < 2; ++n) {
            if (!scan[n] || iio_channel_get_index(scan[n]) != n) throw std::runtime_error("IIO_SCAN_INDEX");
            auto f = iio_channel_get_data_format(scan[n]);
            if (f->length != 16 || f->bits != 16 || f->shift || !f->is_signed || f->is_be || f->repeat != 1)
                throw std::runtime_error("IIO_SCAN_FORMAT");
            iio_channel_enable(scan[n]);
        }
        if (iio_device_get_sample_size(device) != 4) throw std::runtime_error("IIO_SCAN_SIZE");
        // At dual 8 MSPS, sixteen 1 MiB blocks per channel cover only about 0.52 s. Board health and
        // guard RPCs can take 0.3-0.45 s; 32 blocks provide about 1.05 s of scheduling margin.
        // Dual-channel maximum is 64 MiB; retain warmup/ADC guard, overload checks and exact counts; restore four buffers at exit.
        if (iio_device_set_kernel_buffers_count(device,32) < 0) throw std::runtime_error("IIO_KERNEL_BUFFERS");
        changed = true;
        if (!d->callback.permitted()) throw std::runtime_error("OWNER_PERMISSION_BEFORE_BUFFER");
        if (d->callback.stopping()) throw std::runtime_error("START_CANCELLED");
        auto* buffer = iio_device_create_buffer(device,d->samples,false);
        if (!buffer) throw std::runtime_error("IIO_CREATE_BUFFER: " + std::to_string(errno));
        {
            std::lock_guard<std::mutex> lock(d->io_mutex);
            d->buffer = buffer;
        }
        {
            std::lock_guard<std::mutex> lock(d->window_mutex);
            d->callback.guard("begin");
        }
        // Calculate warmup by sample count; valid short refills must not shorten settling.
        std::uint64_t warming = 34ULL*d->samples;
        auto last_data = std::chrono::steady_clock::now();
        std::uint64_t index = 0;
        while (!d->callback.stopping() && !d->finish_requested) {
            if (!d->callback.permitted()) throw std::runtime_error("OWNER_PERMISSION_EXPIRED");
            auto size = iio_buffer_refill(buffer);
            if (d->callback.stopping()) break;
            if (size < 0) throw std::runtime_error("IIO_REFILL: " + std::to_string(size));
            if (d->callback.refill) d->callback.refill(static_cast<std::size_t>(size));
            auto* base = static_cast<unsigned char*>(iio_buffer_start(buffer));
            const auto step = iio_buffer_step(buffer);
            const auto span = reinterpret_cast<std::intptr_t>(iio_buffer_end(buffer))-reinterpret_cast<std::intptr_t>(base);
            const auto i_offset = reinterpret_cast<std::intptr_t>(iio_buffer_first(buffer,scan[0]))-reinterpret_cast<std::intptr_t>(base);
            const auto q_offset = reinterpret_cast<std::intptr_t>(iio_buffer_first(buffer,scan[1]))-reinterpret_cast<std::intptr_t>(base);
            // libiio 0.23 network refills return actual bytes and can end before READBUF's terminating zero line.
            // Never pad or reuse stale blocks; incomplete IQ/invalid scan still fails strictly.
            if (size > static_cast<std::ptrdiff_t>(d->samples*4) || size%4 || step != 4 || span != size ||
                (size && (!base || i_offset != 0 || q_offset != 2))) {
                std::ostringstream detail;
                detail << "IIO_BUFFER_GEOMETRY bytes=" << size << " requested=" << d->samples*4
                       << " step=" << step << " span=" << span << " i_offset=" << i_offset
                       << " q_offset=" << q_offset << " after_samples=" << index;
                throw std::runtime_error(detail.str());
            }
            const auto now = std::chrono::steady_clock::now();
            if (!size) {
                if (now-last_data >= std::chrono::seconds(2)) throw std::runtime_error("IIO_EMPTY_REFILL_DEADLINE");
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                continue;
            }
            last_data = now;
            const auto available = static_cast<std::uint64_t>(size/4);
            if (warming) {
                warming = available >= warming ? 0 : warming-available;
                if (!warming) {
                    std::lock_guard<std::mutex> lock(d->window_mutex);
                    if (d->callback.stopping()) break;
                    d->callback.guard("arm"); d->armed = true; d->callback.ready();
                }
                continue;
            }
            if (!d->callback.permitted()) throw std::runtime_error("OWNER_PERMISSION_EXPIRED_AFTER_REFILL");
            const auto count = d->limit ? std::min<std::uint64_t>(available,d->limit-index) : available;
            sample_block values(count);
            for (std::size_t n = 0; n < count; ++n) {
                // Decode LE explicitly without unaligned pointers or host short-aliasing assumptions.
                auto i = int(base[n*4]) | (int(base[n*4+1]) << 8);
                auto q = int(base[n*4+2]) | (int(base[n*4+3]) << 8);
                if (i >= 32768) i -= 65536;
                if (q >= 32768) q -= 65536;
                values[n] = {float(i)/32768.0f,float(q)/32768.0f};
            }
            d->callback.deliver(std::move(values));
            index += count;
            if (d->limit && index == d->limit) break;
        }
        finish_window();
    } catch (const std::exception& error) {
        if (!d->callback.stopping()) d->callback.fail(error.what());
    }
    // Serialize cancel with destroy so cancellation cannot access a dangling buffer.
    {
        std::lock_guard<std::mutex> window_lock(d->window_mutex);
        std::lock_guard<std::mutex> io_lock(d->io_mutex);
        if (d->buffer) { iio_buffer_destroy(d->buffer); d->buffer = nullptr; }
    }
    if (context) {
        if (changed && iio_device_set_kernel_buffers_count(device,4) < 0)
            d->callback.fail("IIO_KERNEL_BUFFERS_RESTORE");
        iio_context_destroy(context);
    }
}
} // namespace gr::logicrf
