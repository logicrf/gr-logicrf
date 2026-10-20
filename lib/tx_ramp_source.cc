/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file tx_ramp_source.cc
 * @brief Generate finite GNU cf32 test input bound to physical channel and owner seed.
 * @details No hardware operations; distinct channel sequences with exactly reversible IQ16/32768 conversion.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.5.0
 * @date 2026-10-16
 * @note Board SSI checks and Sink input signatures independently verify actual consumption.
 ******************************************************************************/
#include <gnuradio/logicrf/tx_ramp_source.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <algorithm>
namespace gr::logicrf {
class ramp_source_impl final : public tx_ramp_source {
public:
    ramp_source_impl(device::sptr device,unsigned channel)
        : gr::sync_block("h102_tx_ramp_source",gr::io_signature::make(0,0,0),
                         gr::io_signature::make(1,1,sizeof(gr_complex))),board(std::move(device)),channel(channel) {
        if(!board || board->options().direction!="tx" || channel<1 || channel>2)
            throw std::invalid_argument("TX_RAMP_DEVICE_OR_CHANNEL");
    }
    bool start() override {index=0;initialized=false;return true;}
    int work(int count,gr_vector_const_void_star&,gr_vector_void_star& outputs) override {
        if(!board->error().empty()) return WORK_DONE;
        if(!initialized) {auto o=board->options();seed=o.usb_tx_seed^(channel==2?0x6d2b79f5U:0U);limit=o.tx_samples;initialized=true;}
        if(limit && index==limit) return WORK_DONE;
        if(limit) count=static_cast<int>(std::min<std::uint64_t>(count,limit-index));
        auto* output=static_cast<gr_complex*>(outputs[0]);
        auto value=seed+static_cast<std::uint32_t>(index)*0x9e3779b9U;
        for(int n=0;n<count;++n) {
            auto word=value^0xc0000000U;
            int i=word&65535U,q=word>>16;if(i>=32768)i-=65536;if(q>=32768)q-=65536;
            output[n]={float(i)/32768.0f,float(q)/32768.0f};value+=0x9e3779b9U;
        }
        index+=count;return count;
    }
private:
    device::sptr board;unsigned channel;std::uint32_t seed=0;
    std::uint64_t index=0,limit=0;bool initialized=false;
};
tx_ramp_source::sptr tx_ramp_source::make(device::sptr board,unsigned channel)
{return gnuradio::make_block_sptr<ramp_source_impl>(std::move(board),channel);}
}
