/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_rx_source.cc
 * @brief Connect shared Device queues to GNU Radio with initial identity tags and error events.
 * @details work copies C++ complex samples only; tags use output-item offsets, never byte offsets.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note IIO/USB require native ownership; initialization errors use events/EOS.
 ******************************************************************************/
#include <gnuradio/logicrf/h102_rx_source.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <pmt/pmt.h>

namespace gr::logicrf {
class source_impl final : public h102_rx_source {
public:
    source_impl(device::sptr board, unsigned channel)
        : gr::sync_block("h102_rx_source", gr::io_signature::make(0, 0, 0),
                         gr::io_signature::make(1, 1, sizeof(gr_complex))),
          board(std::move(board)), channel(channel)
    {
        if (!this->board) throw std::invalid_argument("null device");
        if (this->board->options().direction!="rx") throw std::invalid_argument("RX_SOURCE_REQUIRES_RX_DEVICE");
        this->board->attach(channel);
        // GNU 3.10.12 TPB turns an empty inputless Source return into BLKD_IN with a default 250 ms sleep.
        // Private-queue notifications cannot wake TPB; cap empty-read retry at 1 ms while read itself waits at most 25 ms.
        // Do not poll when data arrives normally; retain USB's 20 ms overload check.
        set_blkd_input_timer_value(1);
        message_port_register_out(pmt::mp("events"));
        message_port_register_in(pmt::mp("command"));
        set_msg_handler(pmt::mp("command"), [this](pmt::pmt_t command) { control(command); });
    }
    ~source_impl() override { board->detach(channel); }
    bool start() override
    {
        first = true;usb_backend=board->options().backend=="usb";
        try { board->enter(channel); }
        catch (const std::exception& error) { board->invalidate(error.what()); }
        // start runs in scheduler threads; use explicit work error/EOS to stop the graph without stranding downstream blocks.
        return true;
    }
    bool stop() override { board->leave(channel); return true; }
    int work(int count, gr_vector_const_void_star&, gr_vector_void_star& outputs) override
    {
        std::uint64_t index = 0;
        usb_frame_metadata wire{};bool frame_start=false;
        auto got = board->read(channel, static_cast<gr_complex*>(outputs[0]), count, index,&wire,&frame_start);
        if (got < 0) {
            if (got == -2) {
                auto event = pmt::dict_add(pmt::make_dict(), pmt::mp("error"), pmt::intern(board->error()));
                message_port_pub(pmt::mp("events"), event);
            }
            return WORK_DONE;
        }
        if(got && frame_start && usb_backend) {
            auto meta=pmt::make_dict();
            const std::pair<const char*,std::uint64_t> values[]={
                {"first_sample_index",wire.first},{"frame_samples",wire.samples},{"frame_sequence",wire.sequence},
                {"stream_id",wire.stream_id},{"device_id",wire.device_id},{"configuration_id",wire.config_id},
                {"time_epoch",wire.time_epoch},{"utc_anchor_seconds",wire.anchor_seconds},
                {"offset_ticks",wire.offset_ticks},{"tick_hz",wire.clock_hz},{"time_valid",wire.time_valid},
                {"channel",wire.channel},{"rate_num",wire.rate_num},{"rate_den",wire.rate_den},
                {"center_frequency_hz",wire.center_hz},{"bandwidth_hz",wire.bandwidth_hz}};
            for(const auto& v:values) meta=pmt::dict_add(meta,pmt::mp(v.first),pmt::from_uint64(v.second));
            meta=pmt::dict_add(meta,pmt::mp("gain_mode"),pmt::from_uint64(wire.gain_mode));
            meta=pmt::dict_add(meta,pmt::mp("gain_mdb"),pmt::from_long(wire.gain_mdb));
            add_item_tag(0,nitems_written(0),pmt::mp("h102_rx_frame"),meta);
            // Standard rx_time refers to the frame's first sample; retain original integers; RF delay is uncalibrated.
            add_item_tag(0,nitems_written(0),pmt::mp("rx_time"),pmt::make_tuple(
                pmt::from_uint64(wire.anchor_seconds),pmt::from_double(double(wire.offset_ticks)/wire.clock_hz)));
        }
        if (got && first) {
            auto options = board->options();
            add_item_tag(0, nitems_written(0), pmt::mp("rx_rate"), pmt::from_double(options.sample_rate));
            add_item_tag(0, nitems_written(0), pmt::mp("rx_freq"), pmt::from_double(options.center_frequency));
            auto meta = pmt::dict_add(pmt::make_dict(), pmt::mp("backend"), pmt::intern(options.backend));
            meta = pmt::dict_add(meta, pmt::mp("channel"), pmt::from_long(channel));
            meta = pmt::dict_add(meta, pmt::mp("configuration"), pmt::intern(options.configuration));
            if (options.backend == "mock")
                meta = pmt::dict_add(meta, pmt::mp("first_sample_index"), pmt::from_uint64(index));
            else {
                meta = pmt::dict_add(meta,pmt::mp("output_sample_index"),pmt::from_uint64(index));
                meta = pmt::dict_add(meta,pmt::mp("sample_index_kind"),pmt::mp(options.backend=="usb"?"native_adc_frame_index":"host_output_not_adc"));
                meta = pmt::dict_add(meta,pmt::mp("boot_id"),pmt::intern(options.boot_id));
                meta = pmt::dict_add(meta,pmt::mp("rf_bandwidth"),pmt::from_double(options.rf_bandwidth));
                meta = pmt::dict_add(meta,pmt::mp("active_info_json"),pmt::intern(options.metadata_json));
            }
            add_item_tag(0, nitems_written(0), pmt::mp("h102_config"), meta);
            first = false;
        }
        return static_cast<int>(got);
    }
private:
    /** @brief Message thread handles short control only; reject unsupported hardware operations without blocking work. */
    void control(pmt::pmt_t request)
    {
        auto response = pmt::make_dict();
        try {
            if (!pmt::is_dict(request)) throw std::invalid_argument("COMMAND_MUST_BE_DICT");
            auto value = pmt::dict_ref(request, pmt::mp("action"), pmt::PMT_NIL);
            if (!pmt::is_symbol(value)) throw std::invalid_argument("COMMAND_ACTION_REQUIRED");
            auto action = pmt::symbol_to_string(value);
            if (action == "tune") {
                if (board->options().backend != "mock") throw std::runtime_error("BACKEND_NOT_IMPLEMENTED");
                auto frequency = pmt::dict_ref(request, pmt::mp("frequency_hz"), pmt::PMT_NIL);
                if (!pmt::is_real(frequency) && !pmt::is_integer(frequency))
                    throw std::invalid_argument("FREQUENCY_REQUIRED_HZ");
                board->set_center_frequency(pmt::is_integer(frequency)
                    ? static_cast<double>(pmt::to_long(frequency)) : pmt::to_double(frequency));
            } else if (action != "status") throw std::invalid_argument("UNKNOWN_COMMAND");
            auto options = board->options();
            response = pmt::dict_add(response, pmt::mp("status"), pmt::mp("ok"));
            response = pmt::dict_add(response, pmt::mp("backend"), pmt::intern(options.backend));
            response = pmt::dict_add(response, pmt::mp("rx_rate"), pmt::from_double(options.sample_rate));
            response = pmt::dict_add(response, pmt::mp("rx_freq"), pmt::from_double(options.center_frequency));
            response = pmt::dict_add(response, pmt::mp("channel"), pmt::from_long(channel));
            response = pmt::dict_add(response, pmt::mp("produced"), pmt::from_uint64(board->produced()));
        } catch (const std::exception& error) {
            response = pmt::dict_add(response, pmt::mp("status"), pmt::mp("error"));
            response = pmt::dict_add(response, pmt::mp("error"), pmt::intern(error.what()));
        }
        message_port_pub(pmt::mp("events"), response);
    }
    device::sptr board;
    unsigned channel;
    bool first = true,usb_backend=false;
};
h102_rx_source::sptr h102_rx_source::make(device::sptr board, unsigned channel)
{
    return gnuradio::make_block_sptr<source_impl>(std::move(board), channel);
}
} // namespace gr::logicrf
