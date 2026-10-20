/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_replay_tagger.cc
 * @brief Restore native integer frame/configuration/time tags from 128-byte H1MI replay records.
 * @details Retain one index entry at a time with GNU file-sample offsets; IQ uses C++ memcpy only.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-17
 * @note IIO time is relative; retain original integers without generating host receive UTC.
 ******************************************************************************/
#include <gnuradio/logicrf/h102_replay_tagger.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <array>
#include <atomic>
#include <cmath>
#include <cstring>
#include <fstream>
#include <mutex>
#include <stdexcept>

namespace gr::logicrf {
/** @brief Scheduler owns index fd; mutex snapshots deliver status errors to control. */
class replay_tagger_impl final : public h102_replay_tagger {
public:
    explicit replay_tagger_impl(unsigned channel)
        : gr::sync_block("h102_replay_tagger",gr::io_signature::make(1,1,sizeof(gr_complex)),
                         gr::io_signature::make(1,1,sizeof(gr_complex))),channel(channel)
    {
        if(channel<1 || channel>2)throw std::invalid_argument("REPLAY_CHANNEL");
        set_tag_propagation_policy(TPP_DONT);
    }
    void configure(const std::string& path,const std::string& origin,double fs,double fc,const std::string& config,
                   const std::string& boot,const std::string& info) override
    {
        if(running || (origin!="usb" && origin!="iio" && origin!="mock") ||
           !std::isfinite(fs) || fs<=0 || !std::isfinite(fc) || (origin=="usb" && path.empty()))
            throw std::invalid_argument("REPLAY_CONFIG_REQUIRES_STOPPED_VALID_METADATA");
        index=path;backend=origin;rate=fs;frequency=fc;configuration=config;boot_id=boot;active_info_json=info;configured=true;
    }
    bool start() override
    {
        running=true;first=true;has_row=false;expected=0;sequence=0;
        {std::lock_guard<std::mutex> lock(mutex);failure.clear();}
        if(!configured){fail("REPLAY_NOT_CONFIGURED");return true;}
        if(backend=="usb") {
            input.clear();input.open(index,std::ios::binary);
            if(!input)fail("REPLAY_INDEX_OPEN");else next();
        }
        return true;
    }
    bool stop() override {input.close();running=false;return true;}
    std::string error() const override {std::lock_guard<std::mutex> lock(mutex);return failure;}
    int work(int count,gr_vector_const_void_star& inputs,gr_vector_void_star& outputs) override
    {
        if(!error().empty())return WORK_DONE;
        if(first) {
            auto config=pmt::make_dict();
            config=pmt::dict_add(config,pmt::mp("backend"),pmt::intern("replay"));
            config=pmt::dict_add(config,pmt::mp("original_backend"),pmt::intern(backend));
            config=pmt::dict_add(config,pmt::mp("channel"),pmt::from_long(channel));
            config=pmt::dict_add(config,pmt::mp("configuration"),pmt::intern(configuration));
            config=pmt::dict_add(config,pmt::mp("original_boot_id"),pmt::intern(boot_id));
            config=pmt::dict_add(config,pmt::mp("original_active_info_json"),pmt::intern(active_info_json));
            config=pmt::dict_add(config,pmt::mp("time_quality"),pmt::intern(backend=="usb"?
                "device_digital_valid_utc_uncalibrated":"relative_only"));
            config=pmt::dict_add(config,pmt::mp("sample_index_kind"),pmt::intern(
                backend=="usb"?"native_adc_frame_index":backend=="iio"?"host_output_not_adc":"synthetic"));
            add_item_tag(0,nitems_written(0),pmt::mp("h102_config"),config);
            add_item_tag(0,nitems_written(0),pmt::mp("rx_rate"),pmt::from_double(rate));
            add_item_tag(0,nitems_written(0),pmt::mp("rx_freq"),pmt::from_double(frequency));
            first=false;
        }
        const auto begin=nitems_read(0),end=begin+count;
        if(backend=="usb" && (!has_row && begin>=expected)) {fail("REPLAY_INDEX_COVERAGE");return WORK_DONE;}
        while(has_row && field(8,8)<end) {
            const auto offset=field(8,8);
            if(offset<begin){fail("REPLAY_TAG_OFFSET_REGRESSION");return WORK_DONE;}
            auto metadata=pmt::make_dict();
            struct item {const char* key;unsigned offset,bytes;};
            const item items[]={ {"first_sample_index",16,8},{"frame_samples",48,4},
                {"frame_sequence",40,4},{"stream_id",56,8},{"device_id",64,8},
                {"configuration_id",52,4},{"time_epoch",44,4},{"utc_anchor_seconds",24,8},
                {"offset_ticks",32,4},{"tick_hz",36,4},{"time_valid",6,1},{"channel",5,1},
                {"rate_num",72,8},{"rate_den",80,4},{"center_frequency_hz",88,8},
                {"bandwidth_hz",84,4},{"gain_mode",7,1}};
            for(const auto& item:items)metadata=pmt::dict_add(metadata,pmt::mp(item.key),pmt::from_uint64(field(item.offset,item.bytes)));
            metadata=pmt::dict_add(metadata,pmt::mp("gain_mdb"),pmt::from_long(static_cast<std::int32_t>(field(96,4))));
            add_item_tag(0,nitems_written(0)+offset-begin,pmt::mp("h102_rx_frame"),metadata);
            if(field(6,1) && field(36,4))add_item_tag(0,nitems_written(0)+offset-begin,pmt::mp("rx_time"),
                pmt::make_tuple(pmt::from_uint64(field(24,8)),pmt::from_double(double(field(32,4))/field(36,4))));
            next();
            if(!error().empty())return WORK_DONE;
        }
        if(backend=="usb" && !has_row && end>expected){fail("REPLAY_IQ_EXCEEDS_INDEX");return WORK_DONE;}
        std::memcpy(outputs[0],inputs[0],count*sizeof(gr_complex));return count;
    }
private:
    std::uint64_t field(unsigned offset,unsigned bytes) const
    {
        std::uint64_t result=0;for(unsigned i=0;i<bytes;++i)result|=std::uint64_t(row[offset+i])<<(8*i);
        return result;
    }
    void fail(const std::string& value) {std::lock_guard<std::mutex> lock(mutex);if(failure.empty())failure=value;}
    /** @brief Check version/channel/offset/frame geometry/sequence/time sequentially without caching the full file. */
    void next()
    {
        input.read(reinterpret_cast<char*>(row.data()),row.size());
        if(!input.gcount() && input.eof()){has_row=false;return;}
        if(input.gcount()!=128 || std::memcmp(row.data(),"H1MI",4) || row[4]!=1 || row[5]!=channel ||
           !row[6] || !field(36,4) || !field(44,4) || field(48,4)!=2048 || field(8,8)!=expected ||
           field(16,8)!=expected || field(40,4)!=static_cast<std::uint32_t>(sequence) || !field(80,4) ||
           double(field(72,8))/field(80,4)!=rate || field(88,8)!=frequency) {
            has_row=false;fail("REPLAY_INDEX_CONTRACT");return;
        }
        expected+=2048;++sequence;has_row=true;
    }
    unsigned channel;
    std::string index,backend,configuration,failure,boot_id,active_info_json;
    double rate=0,frequency=0;
    bool configured=false,first=true,has_row=false;
    std::atomic<bool> running{false};
    std::uint64_t expected=0,sequence=0;
    std::array<unsigned char,128> row{};
    std::ifstream input;
    mutable std::mutex mutex;
};
h102_replay_tagger::sptr h102_replay_tagger::make(unsigned channel)
{return gnuradio::make_block_sptr<replay_tagger_impl>(channel);}
} // namespace gr::logicrf
