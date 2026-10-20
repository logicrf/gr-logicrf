/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file qa_tx_engine.cc
 * @brief Independently verify per-sample TX conversion, backpressure, faults and cancellation receipts.
 * @details Check all 65536 IQ16 encodings and independent 64-bit signatures without boards.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#include <gnuradio/logicrf/device.h>
#include <chrono>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>
#include <vector>
using namespace gr::logicrf;
static void require(bool value,const char* message) {if(!value) throw std::runtime_error(message);}
template <class F> static void rejects(F action,const char* reason)
{
    try {action();} catch(const std::exception& error) {
        require(std::string(error.what()).find(reason)!=std::string::npos,error.what());return;
    }
    throw std::runtime_error(std::string("missing rejection: ")+reason);
}
static std::uint64_t signature(const std::vector<std::uint32_t>& data)
{
    std::uint64_t result=0;
    for(auto word:data) result=((result<<7)|(result>>57))^word;
    return result;
}
int main()
{
    try {
        device_options o;o.direction="tx";o.tx_samples=65536;o.frame_samples=257;
        o.queue_frames=2;o.sample_rate=1e6;
        auto board=device::make(o);board->attach(1);board->attach(2);
        std::vector<std::complex<float>> iq;std::vector<std::uint32_t> expected;
        for(int code=-32768;code<=32767;++code) {
            iq.emplace_back(float(code)/32768.0f,float(-1-code)/32768.0f);
            expected.push_back(std::uint32_t(static_cast<std::uint16_t>(code)) |
                std::uint32_t(static_cast<std::uint16_t>(-1-code))<<16);
        }
        // Reuse one Device twice, interleave channel chunks and retain the final 255 samples.
        for(int run=0;run<2;++run) {
            board->enter(1);board->enter(2);
            rejects([&]{board->set_center_frequency(200e6);},"STOP_REQUIRED");
            std::size_t offsets[2]={};
            while(offsets[0]<iq.size() || offsets[1]<iq.size()) {
                for(unsigned ch=1;ch<=2;++ch) if(offsets[ch-1]<iq.size()) {
                    const auto count=std::min<std::size_t>(113,iq.size()-offsets[ch-1]);
                    auto got=board->write(ch,iq.data()+offsets[ch-1],count);
                    require(got>=0,"unexpected TX failure");offsets[ch-1]+=got;
                }
            }
            board->leave(1);board->leave(2);
            require(board->error().empty(),board->error().c_str());
            for(unsigned ch=1;ch<=2;++ch) {
                const auto s=board->tx_stats(ch);
                require(s.at("accepted_samples")==65536 && s.at("mock_consumed_samples")==65536,"exact finite count");
                require(s.at("signature")==signature(expected),"IQ16 roundtrip/order signature");
                require(s.at("discarded_samples")==0 && s.at("finite_complete")==1,"finite drain");
                require(s.at("peak_queued_samples")<=514,"bounded TX memory");
            }
            board->set_center_frequency(200e6);
        }
        std::cout<<"PASS all-IQ16-codes dual-tail backpressure restart\n";

        o.tx_samples=5;o.frame_samples=8;
        auto rounding=device::make(o);rounding->attach(1);rounding->enter(1);
        std::complex<float> edge[]={{-1,1},{0,0},{.5f/32768,-.5f/32768},
            {32767.0f/32768,-32768.0f/32768},{1,-1}};
        require(rounding->write(1,edge,5)==5,"edge acceptance");rounding->leave(1);
        auto s=rounding->tx_stats(1);
        require(s.at("signature")==signature({0x7fff8000,0,0xffff0001,0x80007fff,0x80007fff}),"rounding/LE sign");
        require(s.at("saturated_components")==2,"positive full scale saturation accounting");
        std::cout<<"PASS rounding saturation signed extrema\n";

        for(float bad:{std::numeric_limits<float>::quiet_NaN(),std::numeric_limits<float>::infinity(),1.01f}) {
            auto invalid=device::make(o);invalid->attach(1);invalid->enter(1);
            std::complex<float> data[]={{0,0},{bad,0}};
            require(invalid->write(1,data,2)==-2,"invalid IQ accepted");
            require(invalid->tx_stats(1).at("accepted_samples")==0,"invalid fragment partially consumed");
            invalid->leave(1);require(invalid->error().find("TX_INVALID_IQ")!=std::string::npos,"fault lost");
        }
        std::cout<<"PASS atomic invalid-input rejection\n";

        o.tx_samples=100000;o.frame_samples=256;o.sample_rate=192000;
        auto starved=device::make(o);starved->attach(1);starved->enter(1);
        std::vector<std::complex<float>> zeros(256);
        require(starved->write(1,zeros.data(),256)==256,"prefill1");
        require(starved->write(1,zeros.data(),256)==256,"prefill2");
        const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(2);
        while(starved->error().empty() && std::chrono::steady_clock::now()<until)
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        require(starved->error().find("STARVED")!=std::string::npos,"starvation not bounded");starved->leave(1);
        auto cancelled=device::make(o);cancelled->attach(1);cancelled->attach(2);
        cancelled->enter(1);cancelled->enter(2);
        require(cancelled->write(1,zeros.data(),100)==100,"partial acceptance");
        require(cancelled->write(2,zeros.data(),50)==50,"second partial acceptance");cancelled->leave(1);
        s=cancelled->tx_stats(1);
        require(s.at("accepted_samples")==100 && s.at("discarded_samples")==100 && !s.at("finite_complete"),"cancel accounting");
        require(cancelled->tx_stats(2).at("discarded_samples")==50,"cross-lane first-stop receipt");
        cancelled->leave(2);
        require(cancelled->error().find("CANCELLED")!=std::string::npos,"cancel reported as normal");
        std::cout<<"PASS starvation and partial cancellation\n";

        o.backend="usb";o.frame_samples=2048;o.tx_samples=16384;o.tx_validation="ramp32";
        auto hardware=device::make(o);hardware->attach(1);
        rejects([&]{hardware->enter(1);},"MANAGED_TX_SESSION_REQUIRED");
        o.backend="iio";hardware=device::make(o);hardware->attach(1);
        rejects([&]{hardware->enter(1);},"MANAGED_TX_SESSION_REQUIRED");
        o.tx_samples=0;o.queue_frames=1;rejects([&]{device::make(o);},"invalid TX");
        std::cout<<"PASS hardware preflight refusal\n";
        return 0;
    } catch(const std::exception& error) {std::cerr<<"FAIL "<<error.what()<<'\n';return 1;}
}
