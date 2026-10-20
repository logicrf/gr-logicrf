/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file fm_audio_sink.cc
 * @brief Play FM audio through PulseAudio backpressure without periodically rebuilding ALSA plugins.
 * @details One GNU worker owns PA context and drives protocol/callbacks through bounded polling. Normal
 *          input is submitted directly without Python/disk or dropped frames for clock correction.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 1.1.0
 * @date 2026-10-17
 * @note Actual service negotiation determines latency; disconnect fails explicitly without reconnecting.
 ******************************************************************************/
#include <gnuradio/logicrf/fm_audio_sink.h>
#include <gnuradio/io_signature.h>
#include <pulse/pulseaudio.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <mutex>
#include <stdexcept>
#include <thread>

namespace gr::logicrf {
namespace {
/** @brief GNU lifecycle/worker thread owns PA handles; callbacks run in its nonblocking mainloop. */
class fm_audio_sink_impl final : public fm_audio_sink {
    unsigned latency_ms_;
    pa_mainloop* loop_=nullptr;
    pa_context* context_=nullptr;
    pa_stream* stream_=nullptr;
    std::atomic<std::uint64_t> samples_{0},underflows_{0},startup_{0},target_bytes_{0},prebuffer_bytes_{0},max_input_gap_us_{0};
    std::chrono::steady_clock::time_point last_input_{};
    bool draining_=false, flushed_=false;
    std::atomic<bool> discard_{false}, playback_started_{false};
    std::atomic<std::uint64_t> discarded_{0}, flush_ns_{0};
    mutable std::mutex error_mutex_;
    std::string error_;

    void fail(const std::string& text) {
        std::lock_guard<std::mutex> lock(error_mutex_);
        if(error_.empty())error_=text;
    }
    void pump() {
        if(pa_mainloop_iterate(loop_,0,nullptr)<0)throw std::runtime_error("FM_AUDIO_MAINLOOP_FAILED");
        if(context_ && !PA_CONTEXT_IS_GOOD(pa_context_get_state(context_)))
            throw std::runtime_error(std::string("FM_AUDIO_CONTEXT:")+pa_strerror(pa_context_errno(context_)));
        if(stream_ && !PA_STREAM_IS_GOOD(pa_stream_get_state(stream_)))
            throw std::runtime_error("FM_AUDIO_STREAM_FAILED");
    }
    /** @brief Bound control/writes by wall-clock deadlines so service loss cannot block stop/recovery indefinitely. */
    void until(const std::function<bool()>& ready,double seconds) {
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::duration<double>(seconds);
        while(!ready()) {
            pump();
            if(std::chrono::steady_clock::now()>deadline)throw std::runtime_error("FM_AUDIO_SERVICE_DEADLINE");
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    void cleanup() noexcept {
        if(stream_) {pa_stream_disconnect(stream_);pa_stream_unref(stream_);stream_=nullptr;}
        if(context_) {pa_context_disconnect(context_);pa_context_unref(context_);context_=nullptr;}
        if(loop_) {pa_mainloop_free(loop_);loop_=nullptr;}
    }
    /** @brief Sole PA thread flushes and awaits acknowledgement; explicit abort must not hide steady-state underflows. */
    void flush_playback() {
        if(flushed_ || !stream_)return;
        pump();draining_=true;
        bool completed=false;
        pa_operation* operation=pa_stream_flush(stream_,[](pa_stream*,int success,void* userdata){
            *static_cast<bool*>(userdata)=success!=0;
        },&completed);
        if(!operation)throw std::runtime_error("FM_AUDIO_FLUSH_UNAVAILABLE");
        try {
            until([&]{return pa_operation_get_state(operation)!=PA_OPERATION_RUNNING;},2);
            if(!completed)throw std::runtime_error("FM_AUDIO_FLUSH_FAILED");
        }catch(...) {pa_operation_cancel(operation);pa_operation_unref(operation);throw;}
        pa_operation_unref(operation);flushed_=true;
        flush_ns_=std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
public:
    explicit fm_audio_sink_impl(unsigned latency_ms)
      :gr::sync_block("fm_audio_sink",gr::io_signature::make(1,1,sizeof(float)),gr::io_signature::make(0,0,0)),latency_ms_(latency_ms) {
        if(latency_ms<100 || latency_ms>2000)throw std::invalid_argument("FM_AUDIO_LATENCY_100_2000MS");
        set_max_noutput_items(4800);
    }
    ~fm_audio_sink_impl() override {cleanup();}
    bool start() override {
        samples_=0;underflows_=0;startup_=0;max_input_gap_us_=0;last_input_={};draining_=false;flushed_=false;discard_=false;discarded_=0;flush_ns_=0;playback_started_=false;
        {std::lock_guard<std::mutex> lock(error_mutex_);error_.clear();}
        try {
            cleanup();loop_=pa_mainloop_new();
            if(!loop_)throw std::runtime_error("FM_AUDIO_MAINLOOP_ALLOCATION");
            context_=pa_context_new(pa_mainloop_get_api(loop_),"LogicRF H102 FM");
            if(!context_ || pa_context_connect(context_,nullptr,PA_CONTEXT_NOAUTOSPAWN,nullptr)<0)
                throw std::runtime_error("FM_AUDIO_SERVICE_UNAVAILABLE");
            until([this]{return pa_context_get_state(context_)==PA_CONTEXT_READY;},5);
            const pa_sample_spec spec{PA_SAMPLE_FLOAT32LE,48000,1};
            stream_=pa_stream_new(context_,"H102 FM mono 48kHz",&spec,nullptr);
            if(!stream_)throw std::runtime_error("FM_AUDIO_STREAM_ALLOCATION");
            pa_stream_set_started_callback(stream_,[](pa_stream*,void* userdata){
                static_cast<fm_audio_sink_impl*>(userdata)->playback_started_=true;
            },this);
            pa_stream_set_underflow_callback(stream_,[](pa_stream*,void* userdata){
                auto* self=static_cast<fm_audio_sink_impl*>(userdata);
                if(self->draining_)return;
                if(self->samples_.load())++self->underflows_;else ++self->startup_;
            },this);
            // tlength sets this stream's buffer only; hardware latency cannot replace margin for burst delivery.
            pa_buffer_attr attr{};
            attr.maxlength=48000*4*2;
            attr.tlength=48000*4*latency_ms_/1000;
            attr.prebuf=attr.tlength;
            attr.minreq=480*4;
            attr.fragsize=static_cast<std::uint32_t>(-1);
            if(pa_stream_connect_playback(stream_,nullptr,&attr,PA_STREAM_NOFLAGS,nullptr,nullptr)<0)
                throw std::runtime_error("FM_AUDIO_CONNECT_PLAYBACK_FAILED");
            until([this]{return pa_stream_get_state(stream_)==PA_STREAM_READY;},5);
            const auto* actual=pa_stream_get_buffer_attr(stream_);
            target_bytes_=actual->tlength;prebuffer_bytes_=actual->prebuf;
            return true;
        }catch(const std::exception& ex) {fail(ex.what());cleanup();return false;}
    }
    int work(int count,gr_vector_const_void_star& input,gr_vector_void_star&) override {
        // GNU may schedule work after failed start; terminate without a handle while retaining a readable error.
        if(!stream_ || !context_ || !loop_)return WORK_DONE;
        const auto now=std::chrono::steady_clock::now();
        if(last_input_.time_since_epoch().count()) {
            const auto gap=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(now-last_input_).count());
            max_input_gap_us_=std::max(max_input_gap_us_.load(),gap);
        }
        last_input_=now;
        try {
            if(discard_.load()) {flush_playback();discarded_+=count;return count;}
            std::size_t writable=0;
            until([&]{writable=pa_stream_writable_size(stream_);return discard_.load() || (writable>=sizeof(float) && writable!=static_cast<std::size_t>(-1));},3);
            if(discard_.load())return 0;
            const auto consumed=std::min<std::size_t>(count,writable/sizeof(float));
            if(pa_stream_write(stream_,input[0],consumed*sizeof(float),nullptr,0,PA_SEEK_RELATIVE)<0)
                throw std::runtime_error("FM_AUDIO_WRITE_FAILED");
            samples_+=consumed;
            pump();
            return static_cast<int>(consumed);
        }catch(const std::exception& ex) {fail(ex.what());return WORK_DONE;}
    }
    bool stop() override {
        // Dispatch pending active-period events first so existing underflows are not attributed to explicit stop.
        if(loop_) {try {pump();} catch(const std::exception& ex) {fail(ex.what());}}
        if(discard_.load()) {
            try {flush_playback();}catch(const std::exception& ex){fail(ex.what());}
            cleanup();return error().empty();
        }
        draining_=true;
        if(stream_ && pa_stream_get_state(stream_)==PA_STREAM_READY) {
            bool completed=false;
            pa_operation* operation=pa_stream_drain(stream_,[](pa_stream*,int success,void* userdata){
                *static_cast<bool*>(userdata)=success!=0;
            },&completed);
            if(operation) {
                try {
                    until([&]{return pa_operation_get_state(operation)!=PA_OPERATION_RUNNING;},5);
                    if(!completed)fail("FM_AUDIO_DRAIN_FAILED");
                }catch(const std::exception& ex) {fail(ex.what());pa_operation_cancel(operation);}
                pa_operation_unref(operation);
            }else fail("FM_AUDIO_DRAIN_UNAVAILABLE");
        }
        cleanup();return error().empty();
    }
    void discard_pending() override {discard_=true;}
    std::map<std::string,std::uint64_t> snapshot() const override {
        return {{"submitted_samples",samples_.load()},{"discarded_input_samples",discarded_.load()},
                {"flush_completed_ns",flush_ns_.load()},{"playback_started",playback_started_.load()},{"underflows",underflows_.load()},
                {"startup_underflows",startup_.load()},{"target_bytes",target_bytes_.load()},
                {"prebuffer_bytes",prebuffer_bytes_.load()},{"rate_hz",48000},{"max_input_gap_us",max_input_gap_us_.load()}};
    }
    std::string error() const override {std::lock_guard<std::mutex> lock(error_mutex_);return error_;}
};
} // namespace
fm_audio_sink::sptr fm_audio_sink::make(unsigned latency_ms) {
    return gnuradio::make_block_sptr<fm_audio_sink_impl>(latency_ms);
}
} // namespace gr::logicrf
