/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_tx_wire.cc
 * @brief Submit multiple DMA blocks in one IIOD request with permission checks and an exact final reply.
 * @details Nonblocking socket checks stop/permission every 100 ms and fails after one second without progress.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-19
 * @note No RF access, retry or padding; outer libiio checks device/scan format first.
 ******************************************************************************/
#include "iio_tx_wire.h"
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <netdb.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

namespace gr::logicrf {
struct iio_tx_wire::impl {
    using clock = std::chrono::steady_clock;
    std::function<void()> check;
    std::mutex mutex;
    int fd = -1;
    bool cancelled = false;
    std::string device;
    std::size_t payload_slice = 4096;

    void reset() noexcept
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (fd >= 0) { ::close(fd); fd = -1; }
    }
    void wait(short events, clock::time_point progress)
    {
        while (true) {
            check();
            if (clock::now() - progress >= std::chrono::seconds(1))
                throw std::runtime_error("IIO_TX_WIRE_PROGRESS_TIMEOUT");
            pollfd request{fd, events, 0};
            const int value = ::poll(&request, 1, 100);
            if (value < 0 && errno == EINTR) continue;
            if (value < 0) throw std::runtime_error("IIO_TX_WIRE_POLL:" + std::to_string(errno));
            if (!value) continue;
            if (request.revents & events) return;
            throw std::runtime_error("IIO_TX_WIRE_DISCONNECTED");
        }
    }
    void send(const void* bytes, std::size_t count, bool payload = false)
    {
        const auto* data = static_cast<const char*>(bytes);
        auto progress = clock::now();
        while (count) {
            check();
            const auto chunk = payload ? std::min(count,payload_slice) : count;
            // EOR controls sender coalescing; avoid PSH on intermediate fragments to preserve receive GRO opportunities.
            // Push commands/final batch fragments immediately; short tails do not depend on cork timeout.
            const int flags = MSG_NOSIGNAL | (payload ? MSG_EOR : 0) |
                              (payload && count > chunk ? MSG_MORE : 0);
            const auto value = ::send(fd, data, chunk, flags);
            if (value > 0) {
                data += value; count -= value; progress = clock::now();
                continue;
            }
            if (value < 0 && errno == EINTR) continue;
            if (value < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                wait(POLLOUT, progress); continue;
            }
            throw std::runtime_error("IIO_TX_WIRE_SEND:" + std::to_string(errno));
        }
    }
    void command(const std::string& value) { send(value.data(), value.size()); }
    std::int64_t response()
    {
        std::string line;
        auto progress = clock::now();
        while (line.size() < 32) {
            check(); char byte = 0;
            const auto value = ::recv(fd, &byte, 1, 0);
            if (value == 1) {
                progress = clock::now();
                if (byte == '\n') {
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    std::int64_t result = 0;
                    const auto parsed = std::from_chars(line.data(), line.data()+line.size(), result);
                    if (line.empty() || parsed.ec != std::errc{} || parsed.ptr != line.data()+line.size())
                        throw std::runtime_error("IIO_TX_WIRE_RESPONSE_FORMAT");
                    return result;
                }
                line += byte; continue;
            }
            if (value < 0 && errno == EINTR) continue;
            if (value < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                wait(POLLIN, progress); continue;
            }
            throw std::runtime_error("IIO_TX_WIRE_RESPONSE_EOF");
        }
        throw std::runtime_error("IIO_TX_WIRE_RESPONSE_TOO_LONG");
    }
};
iio_tx_wire::iio_tx_wire(std::function<void()> check) : d(std::make_unique<impl>())
{
    d->check = std::move(check);
}
iio_tx_wire::~iio_tx_wire() { d->reset(); }
void iio_tx_wire::cancel() noexcept
{
    std::lock_guard<std::mutex> lock(d->mutex);
    d->cancelled = true;
    if (d->fd >= 0) ::shutdown(d->fd, SHUT_RDWR);
}
void iio_tx_wire::open(const std::string& uri, const std::string& device,
                       std::size_t samples, const std::string& mask,
                       std::uint64_t pacing_bytes_per_second)
{
    if (uri.rfind("ip:", 0) != 0 || uri.size() == 3)
        throw std::runtime_error("IIO_TX_WIRE_URI");
    if (device.rfind("iio:device", 0) != 0 || device.size() == 10 ||
        !std::all_of(device.begin()+10, device.end(), [](char c) { return c >= '0' && c <= '9'; }))
        throw std::runtime_error("IIO_TX_WIRE_DEVICE_ID");
    std::string host = uri.substr(3), port = "30431";
    if (host.front() == '[') {
        const auto end = host.find(']');
        if (end == std::string::npos) throw std::runtime_error("IIO_TX_WIRE_IPV6_URI");
        if (end+1 < host.size()) {
            if (host[end+1] != ':') throw std::runtime_error("IIO_TX_WIRE_URI_PORT");
            port = host.substr(end+2);
        }
        host = host.substr(1, end-1);
    } else if (std::count(host.begin(), host.end(), ':') == 1) {
        const auto split = host.find(':'); port = host.substr(split+1); host.resize(split);
    }
    unsigned port_number = 0;
    const auto parsed = std::from_chars(port.data(), port.data()+port.size(), port_number);
    if (host.empty() || parsed.ec != std::errc{} || parsed.ptr != port.data()+port.size() ||
        !port_number || port_number > 65535) throw std::runtime_error("IIO_TX_WIRE_URI_PORT");
    addrinfo hints{}; hints.ai_family = AF_UNSPEC; hints.ai_socktype = SOCK_STREAM;
    addrinfo* addresses = nullptr;
    const auto error = ::getaddrinfo(host.c_str(), port.c_str(), &hints, &addresses);
    if (error) throw std::runtime_error("IIO_TX_WIRE_RESOLVE:" + std::to_string(error));
    std::unique_ptr<addrinfo, decltype(&freeaddrinfo)> cleanup(addresses, freeaddrinfo);
    bool connected = false;
    for (auto* address = addresses; address; address = address->ai_next) {
        d->check();
        const int fd = ::socket(address->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
                                address->ai_protocol);
        if (fd < 0) continue;
        {
            std::lock_guard<std::mutex> lock(d->mutex);
            if (d->cancelled) { ::close(fd); throw std::runtime_error("IIO_TX_CANCELLED"); }
            d->fd = fd;
        }
        int result = ::connect(fd, address->ai_addr, address->ai_addrlen);
        if (result < 0 && errno == EINPROGRESS) {
            d->wait(POLLOUT, impl::clock::now());
            int value = 0; socklen_t length = sizeof(value);
            if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &value, &length) == 0 && !value) result = 0;
        }
        if (!result) { connected = true; break; }
        d->reset();
    }
    if (!connected) throw std::runtime_error("IIO_TX_WIRE_CONNECT");
    const int yes = 1;
    if (::setsockopt(d->fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes)) < 0)
        throw std::runtime_error("IIO_TX_WIRE_NODELAY");
    int mss=0; socklen_t mss_length=sizeof(mss);
    if (::getsockopt(d->fd, IPPROTO_TCP, TCP_MAXSEG, &mss, &mss_length)<0 || mss<=0)
        throw std::runtime_error("IIO_TX_WIRE_MSS");
    // Limit a fragment to one actual MSS to avoid two line-rate segments bursting into a small FIFO.
    // TCP fragments need not align with IQ words; retain a 4 KiB cap even with larger MTU.
    d->payload_slice=std::min<std::size_t>(4096,static_cast<std::size_t>(mss));
    // Pace this TX connection's bursts for protocol/scheduling margin; preserve global network settings.
    if (!pacing_bytes_per_second ||
        ::setsockopt(d->fd, SOL_SOCKET, SO_MAX_PACING_RATE,
                     &pacing_bytes_per_second, sizeof(pacing_bytes_per_second)) < 0)
        throw std::runtime_error("IIO_TX_WIRE_PACING_SET");
    d->device = device;
    d->command("TIMEOUT 500\r\n");
    if (d->response() != 0) throw std::runtime_error("IIO_TX_WIRE_TIMEOUT_SET");
    d->command("OPEN " + device + " " + std::to_string(samples) + " " + mask + "\r\n");
    if (d->response() != 0) throw std::runtime_error("IIO_TX_WIRE_OPEN");
}
void iio_tx_wire::push(const void* bytes, std::size_t count)
{
    if (!count || count%4 || count > 32*1024*1024)
        throw std::runtime_error("IIO_TX_WIRE_BATCH_SIZE");
    d->command("WRITEBUF " + d->device + " " + std::to_string(count) + "\r\n");
    const auto begin = d->response();
    if (begin != 0) throw std::runtime_error("IIO_TX_WRITE_BEGIN:" + std::to_string(begin));
    d->send(bytes, count, true);
    const auto result = d->response();
    if (result != static_cast<std::int64_t>(count))
        throw std::runtime_error("IIO_TX_PUSH_LENGTH:" + std::to_string(result));
}
void iio_tx_wire::close()
{
    d->command("CLOSE " + d->device + "\r\n");
    if (d->response() != 0) throw std::runtime_error("IIO_TX_WIRE_CLOSE");
    d->command("EXIT\r\n"); d->reset();
}
} // namespace gr::logicrf
