/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file stream_probe.cc
 * @brief Count samples/check RAMP16 in native C++ Sink without Python in the data path.
 * @details Retain counters across work and bounded tags/prefixes; ordinary IQ checks finiteness/range only.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note Combine diagnostics with native guard/post; they cannot independently qualify losslessness.
 ******************************************************************************/
#include <gnuradio/logicrf/stream_probe.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <cmath>
#include <mutex>

namespace gr::logicrf {
class probe_impl final : public stream_probe {
public:
    explicit probe_impl(bool ramp16)
        : gr::sync_block("h102_stream_probe",gr::io_signature::make(1,1,sizeof(gr_complex)),gr::io_signature::make(0,0,0)), ramp(ramp16) {}
    bool start() override
    {
        std::lock_guard<std::mutex> lock(mutex);
        stats = {{"samples",0},{"nonfinite",0},{"out_of_range",0},{"ramp_errors",0},{"iq_mismatch",0}};
        stats["generation"]=++generation;
        first.clear(); metadata_tags.clear(); signal.clear(); latest=pmt::make_dict();
        previous_valid=false;usb_expected=0;usb_sequence=0;return true;
    }
    int work(int count,gr_vector_const_void_star& inputs,gr_vector_void_star&) override
    {
        auto values = static_cast<const gr_complex*>(inputs[0]);
        std::lock_guard<std::mutex> lock(mutex);
        double sum_i=0,sum_q=0,power=0,peak=0,clipped=0,finite=0;
        for (int n=0;n<count;++n) {
            const auto value=values[n];
            if (first.size()<16) first.push_back(value);
            if (!std::isfinite(value.real()) || !std::isfinite(value.imag())) { ++stats["nonfinite"]; continue; }
            const double i=value.real(),q=value.imag(),magnitude=i*i+q*q;
            sum_i+=i;sum_q+=q;power+=magnitude;peak=std::max(peak,magnitude);++finite;
            if(std::abs(i)>=32767.0/32768 || std::abs(q)>=32767.0/32768) ++clipped;
            if (value.real() < -1 || value.real() >= 1 || value.imag() < -1 || value.imag() >= 1) ++stats["out_of_range"];
            if (ramp) {
                const auto i=static_cast<std::uint16_t>(std::lround(value.real()*32768));
                const auto q=static_cast<std::uint16_t>(std::lround(value.imag()*32768));
                if (i!=q) ++stats["iq_mismatch"];
                if (previous_valid && (static_cast<std::uint16_t>(i-previous_i)!=1 || static_cast<std::uint16_t>(q-previous_q)!=1))
                    ++stats["ramp_errors"];
                previous_i=i;previous_q=q;previous_valid=true;
            }
        }
        stats["samples"]+=count;
        signal={{"window_samples",double(count)},{"finite_samples",finite},
                {"mean_i",finite?sum_i/finite:0},{"mean_q",finite?sum_q/finite:0},
                {"rms",finite?std::sqrt(power/finite):0},{"peak",std::sqrt(peak)},
                {"clipped_fraction",finite?clipped/finite:0}};
        std::vector<gr::tag_t> incoming;
        get_tags_in_range(incoming,0,nitems_read(0),nitems_read(0)+count);
        for(const auto& tag:incoming) {
            if(metadata_tags.size()<16) metadata_tags.push_back(tag);
            if(tag.key==pmt::mp("h102_config") || tag.key==pmt::mp("h102_rx_frame") ||
               tag.key==pmt::mp("rx_time") || tag.key==pmt::mp("rx_rate") || tag.key==pmt::mp("rx_freq"))
                latest=pmt::dict_add(latest,tag.key,tag.value);
            if(tag.key!=pmt::mp("h102_rx_frame")) continue;
            auto field=[&](const char* key){return pmt::to_uint64(pmt::dict_ref(tag.value,pmt::mp(key),pmt::PMT_NIL));};
            try {
                ++stats["usb_frame_tags"];
                const auto first=field("first_sample_index"),samples=field("frame_samples");
                if(first!=usb_expected || tag.offset!=usb_expected || field("frame_sequence")!=static_cast<std::uint32_t>(usb_sequence) ||
                    !field("time_valid") || !field("time_epoch") || !field("tick_hz")) ++stats["usb_tag_errors"];
                usb_expected=first+samples;++usb_sequence;
                stats["usb_tagged_samples"]+=samples;
            } catch(...) {++stats["usb_tag_errors"];}
        }
        return count;
    }
    std::map<std::string,std::uint64_t> snapshot() const override { std::lock_guard<std::mutex> lock(mutex);return stats; }
    std::vector<gr_complex> prefix() const override { std::lock_guard<std::mutex> lock(mutex);return first; }
    std::vector<gr::tag_t> tags() const override { std::lock_guard<std::mutex> lock(mutex);return metadata_tags; }
    std::map<std::string,double> signal_snapshot() const override { std::lock_guard<std::mutex> lock(mutex);return signal; }
    pmt::pmt_t metadata() const override { std::lock_guard<std::mutex> lock(mutex);return latest; }
private:
    bool ramp;
    mutable std::mutex mutex;
    std::map<std::string,std::uint64_t> stats;
    std::vector<gr_complex> first;
    std::vector<gr::tag_t> metadata_tags;
    std::map<std::string,double> signal;
    pmt::pmt_t latest=pmt::make_dict();
    std::uint16_t previous_i=0,previous_q=0;
    bool previous_valid=false;
    std::uint64_t usb_expected=0,usb_sequence=0;
    std::uint64_t generation=0; ///< Increment across start so old counts cannot be mistaken for new IQ.
};
stream_probe::sptr stream_probe::make(bool ramp16) { return gnuradio::make_block_sptr<probe_impl>(ramp16); }
} // namespace gr::logicrf
