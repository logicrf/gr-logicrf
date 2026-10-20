/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_tx_sink.cc
 * @brief Feed GNU Radio input into finite TX queues and consume only accepted samples.
 * @details Drain on finite completion; early stop retains cancellation counts; errors publish events.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#include <gnuradio/logicrf/h102_tx_sink.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <gnuradio/block_detail.h>
#include <pmt/pmt.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <thread>

namespace gr::logicrf {
/** @brief work accesses C++ queues only; controllers never enter the per-block data path. */
class sink_impl final : public h102_tx_sink {
public:
    sink_impl(device::sptr device,unsigned channel)
        : gr::sync_block("h102_tx_sink",gr::io_signature::make(1,1,sizeof(gr_complex)),
                         gr::io_signature::make(0,0,0)),board(std::move(device)),channel(channel)
    {
        if (!board || board->options().direction!="tx")
            throw std::invalid_argument("TX_SINK_REQUIRES_TX_DEVICE");
        board->attach(channel);
        set_blkd_input_timer_value(1);
        message_port_register_out(pmt::mp("events"));
    }
    ~sink_impl() override {stop_monitor();board->detach(channel);}
    bool start() override
    {
        error_reported=false;monitor_stop=false;
        try {board->enter(channel);} catch (const std::exception& error) {board->invalidate(error.what());}
        // TPB does not call Sink work without input. Independent slow notifications keep starvation visible;
        // applications must shutdown/stop after an error event instead of waiting for more IQ.
        monitor=std::thread([this] {
            std::unique_lock<std::mutex> lock(monitor_mutex);
            while(!monitor_stop) {
                lock.unlock();
                if(!board->error().empty()) {report(false);return;}
                lock.lock();
                monitor_changed.wait_for(lock,std::chrono::milliseconds(20),[this]{return monitor_stop;});
            }
        });
        return true;
    }
    bool stop() override
    {
        stop_monitor();
        // Seal only after natural upstream EOF and full input consumption; external stop remains an interrupted receipt.
        if(!board->options().tx_samples && detail() && detail()->ninputs()==1 &&
           detail()->input(0)->done() && detail()->input(0)->items_available()==0)
            board->finish_tx_input(channel);
        board->leave(channel);report(true);return true;
    }
    int work(int count,gr_vector_const_void_star& inputs,gr_vector_void_star&) override
    {
        if (!board->error().empty()) {report(false);return WORK_DONE;}
        if (board->tx_input_done(channel)) return WORK_DONE;
        const auto accepted=board->write(channel,static_cast<const gr_complex*>(inputs[0]),count);
        if (accepted<0) {report(false);return WORK_DONE;}
        return static_cast<int>(accepted);
    }
private:
    void stop_monitor()
    {
        {std::lock_guard<std::mutex> lock(monitor_mutex);monitor_stop=true;monitor_changed.notify_all();}
        if(monitor.joinable()) monitor.join();
    }
    void report(bool final)
    {
        const auto error=board->error();
        if(!final && (error.empty() || error_reported.exchange(true))) return;
        auto event=pmt::make_dict();
        event=pmt::dict_add(event,pmt::mp("stage"),pmt::mp(final?"stopped":"fault"));
        event=pmt::dict_add(event,pmt::mp("status"),pmt::mp(error.empty()?"finite_complete":"error"));
        event=pmt::dict_add(event,pmt::mp("channel"),pmt::from_long(channel));
        if (!error.empty()) event=pmt::dict_add(event,pmt::mp("error"),pmt::intern(error));
        for (const auto& metric:board->tx_stats(channel))
            event=pmt::dict_add(event,pmt::intern(metric.first),pmt::from_uint64(metric.second));
        message_port_pub(pmt::mp("events"),event);
    }
    device::sptr board;
    unsigned channel;
    std::atomic<bool> error_reported{false};
    std::thread monitor;
    std::mutex monitor_mutex;
    std::condition_variable monitor_changed;
    bool monitor_stop=true;
};
h102_tx_sink::sptr h102_tx_sink::make(device::sptr board,unsigned channel)
{
    return gnuradio::make_block_sptr<sink_impl>(std::move(board),channel);
}
} // namespace gr::logicrf
