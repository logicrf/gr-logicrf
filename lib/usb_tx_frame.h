/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_tx_frame.h
 * @brief Encode actual IQ16 input as finite USB TX v3 frames without generating substitute IQ.
 * @details Encode complete 2048-sample frames only; frozen templates require zero time fields.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>

namespace gr::logicrf {
/** @brief Independent finite channel sequences; advance after encoding succeeds; reject encoding after END. */
class usb_tx_encoder {
public:
    usb_tx_encoder(const std::array<unsigned char,256>& templates,unsigned mask,
                   std::uint64_t samples,std::uint64_t rate,std::uint32_t bandwidth);
    std::array<unsigned char,8320> encode(unsigned channel,const std::uint32_t* iq,std::size_t count);
    bool done(unsigned channel) const;
    /** @brief Seal continuous length before encoding the final frame; never add END to an already encoded frame. */
    void seal(unsigned channel,std::uint64_t samples);
private:
    std::array<unsigned char,256> templates;
    unsigned mask;
    std::array<std::uint64_t,2> frames;
    std::array<std::uint64_t,2> next{};
};
} // namespace gr::logicrf
