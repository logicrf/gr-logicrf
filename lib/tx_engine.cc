/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file tx_engine.cc
 * @brief cf32 quantization, bounded TX queues and finite mock/IIO/USB transport.
 * @details Full queues return actual acceptance; cancellation never repeats tails or counts discarded queues as consumed.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.1
 * @date 2026-10-19
 * @note Mock signatures do not establish USB completion, SSI consumption or RF qualification.
 ******************************************************************************/
#include "tx_engine.h"
#include "usb_tx_frame.h"
#include "iio_tx.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>
#include <atomic>
#include <fstream>
#include <cstring>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace gr::logicrf {
namespace {
unsigned channel_bit(unsigned channel)
{
    if (channel < 1 || channel > 2) throw std::invalid_argument("channel must be 1 or 2");
    return 1U << (channel-1);
}
/** @brief Explicit IQ16 quantization: saturate/count +1; prevalidate and reject other out-of-range segments. */
std::uint16_t quantize(float value, std::uint64_t& saturated)
{
    long code = std::lround(double(value)*32768.0);
    if (code > 32767) {code = 32767; ++saturated;}
    return static_cast<std::uint16_t>(static_cast<std::int16_t>(code));
}
}

struct tx_engine::impl {
    using clock = std::chrono::steady_clock;
    struct lane {
        std::deque<std::vector<std::uint32_t>> queue;
        std::vector<std::uint32_t> partial;
        std::uint64_t accepted=0, consumed=0, signature=0, saturated=0;
        std::uint64_t peak=0, waits=0, discarded=0, push_calls=0, uncertain=0;
        std::uint64_t target=0;
        bool sealed=false,drained=false;
    };
    explicit impl(const device_options& options) : options(options) {}
    device_options options;
    mutable std::mutex mutex;
    std::mutex lifecycle;
    std::condition_variable changed;
    std::array<lane,2> lanes;
    unsigned attached=0, active=0;
    // Small-fragment network stop checks must not wait for the queue lock held during IQ quantization.
    std::atomic<bool> stopping{true};
    bool prepared=false, completed=false, owner_active=false;
    std::string failure;
    std::thread worker;
    std::mutex registry;
    std::array<std::shared_ptr<iio_tx>,2> writers;
    unsigned ready_mask=0;
    std::function<device_options(unsigned)> prepare;
    std::function<void(unsigned,const std::string&)> guard;
    std::function<void()> finish;
    std::function<std::shared_ptr<void>()> unlock_runtime;
    std::atomic<std::int64_t> lease_ns{0};
    static std::int64_t now_ns() {return std::chrono::duration_cast<std::chrono::nanoseconds>(clock::now().time_since_epoch()).count();}
    static bool done(const lane& lane) {return lane.sealed && lane.consumed==lane.target;}

    void fail_locked(const std::string& reason)
    {
        if (failure.empty()) failure=reason;
        stopping=true;
        // First error revokes all queue consumption; receipts still report cancelled tails when two channels stop sequentially.
        for(auto& lane:lanes) lane.discarded=lane.accepted-lane.consumed;
        changed.notify_all();
    }
    void stop_worker()
    {
        auto release=unlock_runtime ? unlock_runtime():nullptr;
        {std::lock_guard<std::mutex> lock(mutex); stopping=true; changed.notify_all();}
        {std::lock_guard<std::mutex> lock(registry);for(auto& writer:writers) if(writer) writer->cancel();}
        if (worker.joinable()) worker.join();
        {std::lock_guard<std::mutex> lock(registry);writers={};}
        if(owner_active) {
            try {finish();} catch(const std::exception& error) {
                std::lock_guard<std::mutex> lock(mutex);
                const auto detail=std::string("TX_OWNER_POST:")+error.what();
                // Retain cancellation cause, but never hide independent post failure behind CANCELLED.
                if(!failure.empty()) failure+=";"+detail;
                else fail_locked(detail);
            }
            owner_active=false;
        }
        std::lock_guard<std::mutex> lock(mutex);
        for (auto& lane:lanes) {
            lane.discarded=lane.accepted-lane.consumed;
            lane.queue.clear();lane.partial.clear();
        }
    }
    /** @brief Independent network writers; release shared ownership only after complete finite submissions. */
    void run_iio(unsigned mask) noexcept
    {
        std::vector<std::thread> threads;
        try {
            for(unsigned ch=0;ch<2;++ch) if(mask&(1U<<ch)) {
                iio_tx::hooks hooks;
                hooks.stopping=[this] {return stopping.load();};
                hooks.permitted=[this] {return lease_ns.load()>now_ns();};
                hooks.fail=[this,ch](const std::string& reason) {
                    std::lock_guard<std::mutex> lock(mutex);
                    if(!failure.empty() && reason.find("RESTORE")!=std::string::npos)
                        failure+=";TX"+std::to_string(ch+1)+":"+reason;
                    fail_locked("TX"+std::to_string(ch+1)+":"+reason);
                };
                hooks.guard=[this,ch](const std::string& stage) {guard(ch+1,stage);};
                hooks.ready=[this,ch,mask] {
                    std::lock_guard<std::mutex> lock(mutex);ready_mask|=1U<<ch;
                    prepared=ready_mask==mask;changed.notify_all();
                };
                hooks.take=[this,ch](std::vector<std::uint32_t>& block) {
                    std::unique_lock<std::mutex> lock(mutex);auto& lane=lanes[ch];
                    const auto until=clock::now()+std::chrono::seconds(5);
                    while(!stopping && lane.queue.empty() && !done(lane)) {
                        if(lease_ns.load()<=now_ns()) throw std::runtime_error("IIO_TX_PERMISSION_EXPIRED");
                        if(clock::now()>until) throw std::runtime_error("IIO_TX_INPUT_STARVED");
                        changed.wait_for(lock,std::chrono::milliseconds(20));
                    }
                    if(stopping || done(lane)) return false;
                    block=std::move(lane.queue.front());lane.queue.pop_front();changed.notify_all();return true;
                };
                hooks.attempting=[this,ch](std::size_t count) {
                    std::lock_guard<std::mutex> lock(mutex);++lanes[ch].push_calls;lanes[ch].uncertain=count;
                };
                hooks.take_available=[this,ch](std::vector<std::uint32_t>& block) {
                    std::lock_guard<std::mutex> lock(mutex);auto& lane=lanes[ch];
                    if(stopping || lane.queue.empty()) return false;
                    block=std::move(lane.queue.front());lane.queue.pop_front();changed.notify_all();return true;
                };
                hooks.submitted=[this,ch](const std::vector<std::uint32_t>& words) {
                    std::lock_guard<std::mutex> lock(mutex);auto& lane=lanes[ch];
                    for(auto word:words) lane.signature=(lane.signature<<7|lane.signature>>57)^word;
                    lane.consumed+=words.size();lane.uncertain=0;
                    if(stopping) lane.discarded=lane.accepted-lane.consumed;
                    changed.notify_all();
                };
                auto writer=std::make_shared<iio_tx>(options.uri,ch+1,options.frame_samples,
                                                     options.sample_rate,std::move(hooks));
                {std::lock_guard<std::mutex> lock(registry);writers[ch]=writer;}
                threads.emplace_back([this,writer,ch]{
                    writer->run();std::lock_guard<std::mutex> lock(mutex);
                    lanes[ch].drained=!stopping && failure.empty() && done(lanes[ch]);changed.notify_all();
                });
            }
        } catch(const std::exception& error) {
            std::lock_guard<std::mutex> lock(mutex);fail_locked(error.what());
        }
        for(auto& thread:threads) if(thread.joinable()) thread.join();
        std::lock_guard<std::mutex> lock(mutex);
        if(!stopping && failure.empty()) {
            for(unsigned ch=0;ch<2;++ch) if((mask&(1U<<ch)) && !done(lanes[ch]))
                fail_locked("IIO_TX_INCOMPLETE_SUBMISSION");
            completed=failure.empty();
        }
        changed.notify_all();
    }
    /** @brief Prefill both channels and simulate consumption at the declared rate without Python/hardware. */
    void run(unsigned mask) noexcept
    {
        try {
            std::unique_lock<std::mutex> lock(mutex);
            const bool prefilled=changed.wait_for(lock,std::chrono::seconds(5),[&] {
                if (stopping) return true;
                for (unsigned i=0;i<2;++i) if (mask&(1U<<i)) {
                    if (lanes[i].queue.size()<2 && !(lanes[i].sealed && lanes[i].accepted==lanes[i].target)) return false;
                }
                return true;
            });
            if (stopping) return;
            if (!prefilled) {fail_locked("TX_PREFILL_DEADLINE");return;}
            prepared=true;changed.notify_all();
            std::array<clock::time_point,2> deadline{clock::now(),clock::now()};
            while (!stopping) {
                unsigned next=2;
                for (unsigned i=0;i<2;++i) {
                    if (!(mask&(1U<<i))) continue;
                    if(done(lanes[i])) {lanes[i].drained=true;changed.notify_all();continue;}
                    if (next==2 || deadline[i]<deadline[next]) next=i;
                }
                if (next==2) {completed=true;changed.notify_all();return;}
                auto& lane=lanes[next];
                if (lane.queue.empty()) {
                    // This is simulated starvation detection, not proof of physical SSI continuity.
                    auto limit=deadline[next]+std::chrono::milliseconds(200);
                    if (!changed.wait_until(lock,limit,[&]{return stopping || !lane.queue.empty() || done(lane);})) {
                        fail_locked("TX_MOCK_INPUT_STARVED channel="+std::to_string(next+1));return;
                    }
                    if (stopping) return;
                    if (done(lane)) continue;
                }
                const auto count=lane.queue.front().size();
                deadline[next]+=std::chrono::duration_cast<clock::duration>(
                    std::chrono::duration<double>(double(count)/options.sample_rate));
                if (changed.wait_until(lock,deadline[next],[&]{return stopping.load();})) return;
                for (auto word:lane.queue.front()) {
                    lane.signature=(lane.signature<<7 | lane.signature>>57)^word;
                }
                lane.consumed+=count;lane.queue.pop_front();changed.notify_all();
            }
        } catch (const std::exception& error) {
            std::lock_guard<std::mutex> lock(mutex);fail_locked(error.what());
        } catch (...) {
            std::lock_guard<std::mutex> lock(mutex);fail_locked("TX_UNKNOWN_WORKER_FAILURE");
        }
    }
    /** @brief One socket alternates complete channel frames; PLAY/END/STOP call Python at low-rate control boundaries only. */
    void run_usb(unsigned mask) noexcept
    {
        int server=-1,peer=-1;bool bound=false;
        try {
            std::array<unsigned char,256> templates{};
            std::ifstream input(options.usb_tx_templates,std::ios::binary);
            input.read(reinterpret_cast<char*>(templates.data()),templates.size());
            if(input.gcount()!=256 || input.peek()!=std::char_traits<char>::eof()) throw std::runtime_error("TX_TEMPLATE_FILE");
            usb_tx_encoder encoder(templates,mask,options.tx_samples,static_cast<std::uint64_t>(options.sample_rate),
                static_cast<std::uint32_t>(options.rf_bandwidth));
            sockaddr_un address{};address.sun_family=AF_UNIX;
            if(options.usb_socket.size()>=sizeof(address.sun_path)) throw std::runtime_error("TX_SOCKET_PATH");
            std::strcpy(address.sun_path,options.usb_socket.c_str());
            server=::socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
            if(server<0 || ::bind(server,reinterpret_cast<sockaddr*>(&address),sizeof(address)))
                throw std::runtime_error("TX_SOCKET_BIND");
            bound=true;
            if(::listen(server,1)) throw std::runtime_error("TX_SOCKET_LISTEN");
            auto check=[this] {
                std::lock_guard<std::mutex> lock(mutex);
                if(stopping) throw std::runtime_error("TX_SOCKET_CANCELLED");
                if(lease_ns.load()<=now_ns()) throw std::runtime_error("TX_PERMISSION_EXPIRED");
            };
            auto deadline=clock::now()+std::chrono::seconds(15);
            while(peer<0) {
                check();pollfd p{server,POLLIN,0};auto r=::poll(&p,1,20);
                if(r<0 && errno==EINTR) continue;
                if(r<0 || clock::now()>deadline) throw std::runtime_error("TX_SOCKET_ACCEPT_DEADLINE");
                if(r>0) peer=::accept4(server,nullptr,nullptr,SOCK_CLOEXEC|SOCK_NONBLOCK);
            }
            ucred credentials{};socklen_t size=sizeof(credentials);
            if(::getsockopt(peer,SOL_SOCKET,SO_PEERCRED,&credentials,&size) || credentials.uid!=::getuid())
                throw std::runtime_error("TX_SOCKET_PEER_UID");
            for(;;) {
                {
                    std::lock_guard<std::mutex> lock(mutex);bool all_done=true;
                    for(unsigned ch=0;ch<2;++ch) if((mask&(1U<<ch)) && !done(lanes[ch])) all_done=false;
                    if(all_done) break;
                }
                for(unsigned ch=0;ch<2;++ch) if(mask&(1U<<ch)) {
                    std::vector<std::uint32_t> iq;
                    {
                        std::unique_lock<std::mutex> lock(mutex);auto& lane=lanes[ch];
                        if(!changed.wait_for(lock,std::chrono::seconds(5),[&]{
                            return stopping || done(lane) || (!lane.queue.empty() && (lane.sealed || lane.queue.size()>1));
                        }))
                            throw std::runtime_error("TX_USB_INPUT_STARVED");
                        if(stopping) throw std::runtime_error("TX_USB_CANCELLED");
                        if(done(lane)) continue;
                        if(!options.tx_samples && lane.sealed) encoder.seal(ch+1,lane.target);
                        iq=std::move(lane.queue.front());lane.queue.pop_front();changed.notify_all();
                    }
                    auto wire=encoder.encode(ch+1,iq.data(),iq.size());std::size_t offset=0;
                    deadline=clock::now()+std::chrono::seconds(5);
                    while(offset<wire.size()) {
                        check();pollfd p{peer,POLLOUT,0};auto r=::poll(&p,1,20);
                        if(r<0 && errno==EINTR) continue;
                        if(r<0 || clock::now()>deadline) throw std::runtime_error("TX_SOCKET_SEND_DEADLINE");
                        if(!r) continue;
                        auto n=::send(peer,wire.data()+offset,wire.size()-offset,MSG_NOSIGNAL|MSG_DONTWAIT);
                        if(n<0 && (errno==EINTR || errno==EAGAIN)) continue;
                        if(n<=0) throw std::runtime_error("TX_SOCKET_CLOSED");
                        offset+=n;
                    }
                    {
                        std::lock_guard<std::mutex> lock(mutex);auto& lane=lanes[ch];
                        for(auto word:iq) lane.signature=(lane.signature<<7|lane.signature>>57)^word;
                        lane.consumed+=iq.size();if(stopping) lane.discarded=lane.accepted-lane.consumed;
                        changed.notify_all();
                    }
                    // Continue supplying data during PLAY round trips; hardware banks cover only hundreds of microseconds.
                    // SSH/sudo latency cannot pause data; the controller confirms readiness separately.
                }
            }
            ::shutdown(peer,SHUT_WR);guard(0,"usb-tx-finish");
            {std::lock_guard<std::mutex> lock(mutex);completed=true;changed.notify_all();}
        } catch(const std::exception& error) {
            {std::lock_guard<std::mutex> lock(mutex);fail_locked(error.what());}
            try {guard(0,"usb-tx-stop");} catch(const std::exception& cleanup) {
                std::lock_guard<std::mutex> lock(mutex);failure+=";TX_STOP:"+std::string(cleanup.what());
            }
        }
        if(peer>=0) ::close(peer);
        if(server>=0) ::close(server);
        if(bound) ::unlink(options.usb_socket.c_str());
    }
};

tx_engine::tx_engine(const device_options& options) : d(std::make_unique<impl>(options)) {}
tx_engine::~tx_engine() {shutdown();}
void tx_engine::attach(unsigned channel)
{
    auto bit=channel_bit(channel);std::lock_guard<std::mutex> lock(d->mutex);
    if (d->active || (d->attached&bit)) throw std::runtime_error("TX_CHANNEL_BUSY");
    d->attached|=bit;
}
void tx_engine::detach(unsigned channel) noexcept
{
    leave(channel);std::lock_guard<std::mutex> lock(d->mutex);
    if (channel>=1 && channel<=2) d->attached&=~(1U<<(channel-1));
}
void tx_engine::enter(unsigned channel)
{
    const auto bit=channel_bit(channel);std::lock_guard<std::mutex> life(d->lifecycle);
    std::unique_lock<std::mutex> lock(d->mutex);
    if (!(d->attached&bit) || (d->active&bit)) throw std::runtime_error("TX_CHANNEL_STATE");
    if (d->options.backend=="iio") {
        const auto count=(d->attached==3?2:1);
        if(d->options.sample_rate*4*count>64000000) throw std::runtime_error("IIO_TX_BUDGET_EXCEEDED");
        if(double(d->options.tx_samples)/d->options.sample_rate>300)
            throw std::runtime_error("IIO_TX_FINITE_DURATION_EXCEEDED");
        if(!d->prepare || !d->guard || !d->finish) throw std::runtime_error("MANAGED_TX_SESSION_REQUIRED");
    }
    if (d->options.backend=="usb") {
        const auto count=(d->attached==3?2:1);
        if(d->options.sample_rate*4*count>320000000) throw std::runtime_error("USB_TX_BUDGET_EXCEEDED");
        if(d->options.frame_samples!=2048 || d->options.tx_samples%16384 ||
           (d->options.tx_validation!="ramp32" && d->options.tx_validation!="none"))
            throw std::runtime_error("TX_USB_DIGITAL_GEOMETRY_OR_PATTERN");
        if(!d->prepare || !d->guard || !d->finish) throw std::runtime_error("MANAGED_TX_SESSION_REQUIRED");
    }
    if (!d->active) {
        if (d->worker.joinable()) throw std::runtime_error("TX_PREVIOUS_WORKER_NOT_JOINED");
        d->failure.clear();d->lanes={};d->stopping=false;d->prepared=d->completed=false;d->ready_mask=0;
        for(auto& lane:d->lanes) {lane.target=d->options.tx_samples;lane.sealed=d->options.tx_samples!=0;}
    }
    d->active|=bit;
    if (d->active!=d->attached) return;
    const auto mask=d->attached;auto expected=d->options;lock.unlock();
    try {
        if(expected.backend=="usb") {
            d->owner_active=true;auto actual=d->prepare(mask);
            if(actual.backend!="usb" || actual.direction!="tx" || actual.sample_rate!=expected.sample_rate ||
                actual.tx_samples!=expected.tx_samples || actual.uri!=expected.uri || actual.tx_validation!=expected.tx_validation ||
                actual.boot_id.empty() || actual.metadata_json.empty() || actual.usb_tx_templates.empty() ||
                actual.usb_socket.empty() || !actual.usb_stream_id || !actual.usb_time_epoch || d->lease_ns.load()<=impl::now_ns())
                throw std::runtime_error("TX_PREPARED_IDENTITY_OR_PERMISSION");
            {std::lock_guard<std::mutex> held(d->mutex);d->options=actual;}
            d->worker=std::thread([this,mask]{d->run_usb(mask);});
        } else if(expected.backend=="iio") {
            d->owner_active=true;auto actual=d->prepare(mask);
            if(actual.backend!="iio" || actual.direction!="tx" || actual.sample_rate!=expected.sample_rate ||
                actual.tx_samples!=expected.tx_samples || actual.uri!=expected.uri ||
                actual.frame_samples!=expected.frame_samples || actual.center_frequency!=expected.center_frequency ||
                actual.boot_id.empty() || actual.metadata_json.empty() || d->lease_ns.load()<=impl::now_ns())
                throw std::runtime_error("TX_PREPARED_IDENTITY_OR_PERMISSION");
            {std::lock_guard<std::mutex> held(d->mutex);d->options=actual;}
            d->worker=std::thread([this,mask]{d->run_iio(mask);});
        } else d->worker=std::thread([this,mask]{d->run(mask);});
    } catch(...) {
        d->stop_worker();std::lock_guard<std::mutex> held(d->mutex);d->active=0;throw;
    }
}
void tx_engine::leave(unsigned channel) noexcept
{
    if (channel<1 || channel>2) return;
    auto release=d->unlock_runtime ? d->unlock_runtime():nullptr;
    std::lock_guard<std::mutex> life(d->lifecycle);
    bool last=false;
    {
        std::unique_lock<std::mutex> lock(d->mutex);
        const auto bit=1U<<(channel-1);if (!(d->active&bit)) return;
        auto& lane=d->lanes[channel-1];
        if (!d->stopping && d->failure.empty() && lane.sealed && lane.accepted==lane.target) {
            const bool usb=d->options.backend=="usb";
            const double seconds=double(lane.accepted-lane.consumed)/d->options.sample_rate+25.0;
            if (!d->changed.wait_for(lock,std::chrono::duration<double>(seconds),[&]{
                return d->stopping || !d->failure.empty() || (usb ? d->completed : lane.drained);
            })) d->fail_locked("TX_DRAIN_DEADLINE");
        } else if (!d->stopping && (!lane.sealed || lane.accepted!=lane.target)) {
            d->fail_locked("TX_CANCELLED_BEFORE_LIMIT channel="+std::to_string(channel));
        }
        d->active&=~bit;last=!d->active;
    }
    if (last) d->stop_worker();
}
void tx_engine::shutdown() noexcept
{
    auto release=d->unlock_runtime ? d->unlock_runtime():nullptr;
    std::lock_guard<std::mutex> life(d->lifecycle);
    {
        std::lock_guard<std::mutex> lock(d->mutex);
        if (d->active && d->failure.empty()) {
            for (unsigned i=0;i<2;++i) if ((d->active&(1U<<i)) && !d->lanes[i].drained && !d->completed) {
                d->fail_locked("TX_CANCELLED_BY_SHUTDOWN");break;
            }
        }
        d->active=0;
    }
    d->stop_worker();
}
std::ptrdiff_t tx_engine::write(unsigned channel,const std::complex<float>* input,std::size_t count)
{
    const auto bit=channel_bit(channel);if (!count) return 0;
    if (!input) throw std::invalid_argument("TX_NULL_INPUT");
    std::unique_lock<std::mutex> lock(d->mutex);
    if (!d->failure.empty()) return -2;
    if (!(d->active&bit) || d->stopping) return -1;
    auto& lane=d->lanes[channel-1];
    if (lane.sealed && lane.accepted==lane.target) return -1;
    if (lane.partial.empty() && lane.queue.size()>=d->options.queue_frames) {
        ++lane.waits;
        if (!d->changed.wait_for(lock,std::chrono::milliseconds(25),[&]{
            return d->stopping || lane.queue.size()<d->options.queue_frames;
        })) return 0;
        if (d->stopping) return d->failure.empty() ? -1 : -2;
    }
    count=std::min<std::size_t>(count,d->options.frame_samples-lane.partial.size());
    if(lane.sealed) count=std::min<std::uint64_t>(count,lane.target-lane.accepted);
    // Check the entire prospective segment first; invalid segments are neither partly queued nor consumed by GNU.
    std::vector<std::uint32_t> converted;converted.reserve(count);std::uint64_t saturated=0;
    for (std::size_t n=0;n<count;++n) {
        if (!std::isfinite(input[n].real()) || !std::isfinite(input[n].imag()) ||
            std::abs(input[n].real())>1 || std::abs(input[n].imag())>1) {
            d->fail_locked("TX_INVALID_IQ channel="+std::to_string(channel));return -2;
        }
        const auto i=quantize(input[n].real(),saturated),q=quantize(input[n].imag(),saturated);
        const auto word=std::uint32_t(i)|(std::uint32_t(q)<<16);
        if(d->options.backend=="usb" && d->options.tx_validation=="ramp32") {
            const std::uint32_t seed=d->options.usb_tx_seed^(channel==2?0x6d2b79f5U:0U);
            const auto expected=(seed+static_cast<std::uint32_t>(lane.accepted+n)*0x9e3779b9U)^0xc0000000U;
            if(word!=expected) {d->fail_locked("TX_DIGITAL_RAMP32_INPUT_MISMATCH");return -2;}
        }
        converted.push_back(word);
    }
    // Reserve exact whole-frame sizes so vector doubling for non-power-of-two frames cannot exceed the data budget.
    if(lane.partial.capacity()<d->options.frame_samples) lane.partial.reserve(d->options.frame_samples);
    lane.partial.insert(lane.partial.end(),converted.begin(),converted.end());lane.saturated+=saturated;
    lane.accepted+=count;
    if (lane.partial.size()==d->options.frame_samples || (lane.sealed && lane.accepted==lane.target)) {
        lane.queue.push_back(std::move(lane.partial));lane.partial.clear();
    }
    lane.peak=std::max(lane.peak,lane.accepted-lane.consumed);
    d->changed.notify_all();return static_cast<std::ptrdiff_t>(count);
}
std::map<std::string,std::uint64_t> tx_engine::stats(unsigned channel) const
{
    channel_bit(channel);std::lock_guard<std::mutex> lock(d->mutex);const auto& lane=d->lanes[channel-1];
    const bool usb=d->options.backend=="usb";
    const bool iio=d->options.backend=="iio";
    return {{"accepted_samples",lane.accepted},{"mock_consumed_samples",!usb&&!iio?lane.consumed:0},
        {"iio_submitted_samples",iio?lane.consumed:0},{"iio_push_calls",lane.push_calls},
        {"iio_uncertain_samples",lane.uncertain},
        {"target_samples",lane.target},{"input_complete",lane.sealed && lane.accepted==lane.target},
        {"socket_sent_samples",usb?lane.consumed:0},{"ssi_verified_samples",usb&&d->completed?lane.consumed:0},
        {"signature",lane.signature},{"saturated_components",lane.saturated},{"peak_queued_samples",lane.peak},
        {"backpressure_waits",lane.waits},{"discarded_samples",lane.discarded},
        {"queued_samples",lane.accepted-lane.consumed-lane.discarded},
        {"finite_complete",impl::done(lane) && (d->options.backend=="mock"||lane.drained||d->completed) && d->failure.empty() ? 1U:0U}};
}
std::string tx_engine::error() const {std::lock_guard<std::mutex> lock(d->mutex);return d->failure;}
bool tx_engine::ready() const {std::lock_guard<std::mutex> lock(d->mutex);return d->prepared && !d->stopping && d->failure.empty();}
bool tx_engine::done(unsigned channel) const
{
    channel_bit(channel);std::lock_guard<std::mutex> lock(d->mutex);
    const auto& lane=d->lanes[channel-1];return lane.sealed && lane.accepted==lane.target;
}
std::uint64_t tx_engine::request_stop()
{
    std::lock_guard<std::mutex> lock(d->mutex);
    if(!d->active || d->stopping || !d->failure.empty()) throw std::runtime_error("TX_STOP_REQUEST_STATE");
    if(d->options.tx_samples) return d->options.tx_samples;
    const std::uint64_t granule=d->options.backend=="usb"?16384:d->options.frame_samples;
    std::uint64_t accepted=0;
    for(unsigned ch=0;ch<2;++ch) if(d->attached&(1U<<ch)) {
        if(d->lanes[ch].sealed) throw std::runtime_error("TX_INPUT_ALREADY_SEALED");
        accepted=std::max(accepted,d->lanes[ch].accepted);
    }
    if(accepted>UINT64_MAX-granule) throw std::runtime_error("TX_SAMPLE_COUNTER_EXHAUSTED");
    const auto target=(accepted/granule+1)*granule;
    for(unsigned ch=0;ch<2;++ch) if(d->attached&(1U<<ch)) {d->lanes[ch].target=target;d->lanes[ch].sealed=true;}
    d->changed.notify_all();return target;
}
void tx_engine::finish_input(unsigned channel)
{
    const auto bit=channel_bit(channel);std::lock_guard<std::mutex> lock(d->mutex);
    if(!(d->active&bit) || d->stopping || d->options.tx_samples) return;
    auto& lane=d->lanes[channel-1];
    if(lane.sealed) return;
    if(d->options.backend=="usb" && (!lane.accepted || lane.accepted%16384)) {
        d->fail_locked("TX_USB_EOF_REQUIRES_16384_SAMPLE_BOUNDARY");return;
    }
    lane.target=lane.accepted;lane.sealed=true;
    if(!lane.partial.empty()) {lane.queue.push_back(std::move(lane.partial));lane.partial.clear();}
    d->changed.notify_all();
}
void tx_engine::invalidate(const std::string& reason)
{
    std::lock_guard<std::mutex> lock(d->mutex);d->fail_locked(reason);
}
void tx_engine::configure(const device_options& options)
{
    std::lock_guard<std::mutex> lock(d->mutex);
    if (d->active) throw std::runtime_error("STOP_REQUIRED_FOR_CONFIGURE");
    d->options=options;
}
device_options tx_engine::options() const {std::lock_guard<std::mutex> lock(d->mutex);return d->options;}
void tx_engine::renew_lease() {d->lease_ns.store(impl::now_ns()+2000000000LL);}
void tx_engine::confirm_playing()
{
    std::lock_guard<std::mutex> lock(d->mutex);
    if(d->options.backend!="usb" || !d->owner_active || d->stopping || !d->active ||
        d->lease_ns.load()<=impl::now_ns()) throw std::runtime_error("TX_PLAY_CONFIRM_STATE");
    d->prepared=true;d->changed.notify_all();
}
void tx_engine::set_session(std::function<device_options(unsigned)> prepare,
    std::function<void(unsigned,const std::string&)> guard,std::function<void()> finish)
{
    std::lock_guard<std::mutex> life(d->lifecycle);std::lock_guard<std::mutex> lock(d->mutex);
    if(d->active) throw std::runtime_error("STOP_REQUIRED_FOR_SESSION");
    d->prepare=std::move(prepare);d->guard=std::move(guard);d->finish=std::move(finish);
}
void tx_engine::set_runtime_unlock(std::function<std::shared_ptr<void>()> unlock) {d->unlock_runtime=std::move(unlock);}
} // namespace gr::logicrf
