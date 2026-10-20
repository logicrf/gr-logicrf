/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_tx_frame.cc
 * @brief Encode complete TX frames, separating native template validation from input IQ writes.
 * @details Use the pinned protocol validator; never substitute historical RAMP generation for caller IQ.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#include "usb_tx_frame.h"
#include "protocol/h102_usb_tx_v3.h"
#include <algorithm>
#include <stdexcept>

namespace gr::logicrf {
usb_tx_encoder::usb_tx_encoder(const std::array<unsigned char,256>& headers,unsigned mask,
    std::uint64_t samples,std::uint64_t rate,std::uint32_t bandwidth)
    : templates(headers),mask(mask),frames{samples/H102_TX_SAMPLES,samples/H102_TX_SAMPLES}
{
    h102_tx_source validated{};
    if (samples%H102_TX_SAMPLES || !h102_tx_initialize_runtime(&validated,headers.data(),mask,
        samples ? samples/H102_TX_SAMPLES : 2,0,rate,bandwidth)) throw std::invalid_argument("TX_USB_TEMPLATE_OR_LENGTH_INVALID");
    for(unsigned ch=0;ch<2;++ch) if(mask&(1U<<ch)) {
        auto frequency=h102_tx_le(headers.data()+ch*128+64,8);
        if(frequency<50000000 || frequency>UINT64_C(5800000000))
            throw std::invalid_argument("TX_USB_FREQUENCY_OUTSIDE_H102_RANGE");
    }
}
std::array<unsigned char,8320> usb_tx_encoder::encode(unsigned channel,const std::uint32_t* iq,std::size_t count)
{
    if (channel<1 || channel>2 || !(mask&(1U<<(channel-1))))
        throw std::invalid_argument("TX_USB_CHANNEL_NOT_SELECTED");
    if (!iq || count!=H102_TX_SAMPLES) throw std::invalid_argument("TX_USB_COMPLETE_FRAME_REQUIRED");
    const auto ch=channel-1;const auto sequence=next[ch];
    if (frames[ch] && sequence>=frames[ch]) throw std::runtime_error("TX_USB_AFTER_END");
    if(sequence>=UINT64_MAX/H102_TX_SAMPLES) throw std::runtime_error("TX_USB_INDEX_EXHAUSTED");
    std::array<unsigned char,8320> result{};
    std::copy_n(templates.data()+ch*128,128,result.data());
    result[10]=sequence==0;result[11]=frames[ch] && sequence+1==frames[ch];
    h102_tx_put(result.data()+20,static_cast<std::uint32_t>(sequence),4);
    h102_tx_put(result.data()+24,sequence*H102_TX_SAMPLES,8);
    for(std::size_t n=0;n<count;++n) h102_tx_put(result.data()+128+4*n,iq[n],4);
    ++next[ch];return result;
}
bool usb_tx_encoder::done(unsigned channel) const
{
    if(channel<1 || channel>2 || !(mask&(1U<<(channel-1))))
        throw std::invalid_argument("TX_USB_CHANNEL_NOT_SELECTED");
    return frames[channel-1] && next[channel-1]==frames[channel-1];
}
void usb_tx_encoder::seal(unsigned channel,std::uint64_t samples)
{
    if(channel<1 || channel>2 || !(mask&(1U<<(channel-1))) ||
       samples<4096 || samples%H102_TX_SAMPLES || samples/H102_TX_SAMPLES<=next[channel-1])
        throw std::invalid_argument("TX_USB_SEAL_BOUNDARY");
    auto& target=frames[channel-1];
    if(target && target!=samples/H102_TX_SAMPLES) throw std::runtime_error("TX_USB_SEAL_CHANGED");
    target=samples/H102_TX_SAMPLES;
}
} // namespace gr::logicrf
