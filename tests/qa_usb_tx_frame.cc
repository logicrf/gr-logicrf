/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file qa_usb_tx_frame.cc
 * @brief Independent TX v3 template checks for actual payloads, finite END and no progression on error.
 * @details Never use production RAMP generation as the data oracle; no USB/RF access.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-16
 ******************************************************************************/
#include "usb_tx_frame.h"
#include <algorithm>
#include <iostream>
#include <stdexcept>
#include <vector>
using namespace gr::logicrf;
static void require(bool value,const char* reason) {if(!value) throw std::runtime_error(reason);}
static void put(unsigned char* p,std::uint64_t v,unsigned count)
{for(unsigned i=0;i<count;++i) p[i]=static_cast<unsigned char>(v>>(i*8));}
static std::uint64_t get(const unsigned char* p,unsigned count)
{std::uint64_t v=0;for(unsigned i=0;i<count;++i) v|=std::uint64_t(p[i])<<(i*8);return v;}
template <class F> static void rejects(F action)
{try {action();} catch(const std::exception&) {return;}throw std::runtime_error("missing rejection");}
static std::array<unsigned char,256> headers(std::uint64_t rate=40000000,std::uint32_t bandwidth=20000000)
{
    std::array<unsigned char,256> t{};
    for(unsigned ch=0;ch<2;++ch) {
        auto* p=t.data()+128*ch;p[0]='H';p[1]='1';p[2]='0';p[3]='2';
        p[4]=3;p[5]=2;p[6]=ch+1;p[7]=1;p[8]=128;
        put(p+16,8192,4);put(p+32,0xabcdef0123456789ULL,8);put(p+40,47,4);
        put(p+48,rate,8);put(p+56,1,4);put(p+60,2048,4);put(p+64,2450000000ULL+ch*10000000,8);
        put(p+72,bandwidth,4);put(p+76,std::uint32_t(-10000),4);put(p+96,0x123456789abcdef0ULL,8);
        std::fill_n(p+104,6,1);
    }
    return t;
}
int main()
{
    try {
        auto h=headers();usb_tx_encoder encoder(h,3,6144,40000000,20000000);
        std::vector<std::uint32_t> iq(2048);
        // Arbitrary non-RAMP32 input; independent channel sequences advance even without alternating calls.
        for(unsigned ch:{2U,1U}) for(unsigned seq=0;seq<3;++seq) {
            for(unsigned n=0;n<2048;++n) iq[n]=(0x12345678U+n*17U)^(ch*0xff00ff00U+seq);
            if(seq==1) rejects([&]{encoder.encode(ch,iq.data(),2047);});
            auto frame=encoder.encode(ch,iq.data(),iq.size());
            auto expected=h;auto* p=expected.data()+128*(ch-1);
            p[10]=seq==0;p[11]=seq==2;put(p+20,seq,4);put(p+24,seq*2048,8);
            require(std::equal(p,p+128,frame.data()),"full 128 byte header mismatch");
            for(unsigned n=0;n<2048;++n) require(get(frame.data()+128+n*4,4)==iq[n],"input IQ replaced or endian error");
            require(encoder.done(ch)==(seq==2),"early/late END");
        }
        rejects([&]{encoder.encode(1,iq.data(),2048);});
        std::cout<<"PASS arbitrary input payload independent channels first/end sequence index\n";
        for(unsigned byte:{4U,5U,10U,11U,13U,20U,24U,44U,48U,56U,60U,72U,80U,88U,92U,104U,110U,112U,127U}) {
            auto bad=h;bad[byte]^=0x80;
            rejects([&]{usb_tx_encoder broken(bad,3,4096,40000000,20000000);});
        }
        // Valid LO is not one magic constant; legitimate frequency changes must not be treated as corrupt templates.
        for(auto frequency:{30000000ULL,5800000001ULL}) {
            auto bad=h;put(bad.data()+64,frequency,8);
            rejects([&]{usb_tx_encoder broken(bad,1,4096,40000000,20000000);});
        }
        for(unsigned byte:{32U,40U,96U}) {
            auto bad=h;bad[128+byte]^=1;
            rejects([&]{usb_tx_encoder broken(bad,3,4096,40000000,20000000);});
        }
        for(auto samples:{2048ULL,4095ULL,4097ULL})
            rejects([&]{usb_tx_encoder broken(h,1,samples,40000000,20000000);});
        usb_tx_encoder only2(h,2,4096,40000000,20000000);
        usb_tx_encoder continuous(h,1,0,40000000,20000000);
        auto first=continuous.encode(1,iq.data(),2048);
        require(first[10]==1 && first[11]==0 && !continuous.done(1),"continuous first");
        continuous.seal(1,4096);
        auto last=continuous.encode(1,iq.data(),2048);
        require(last[11]==1 && continuous.done(1),"continuous explicit END");
        rejects([&]{continuous.seal(1,6144);});
        rejects([&]{only2.encode(1,iq.data(),2048);});
        for(auto rate:{192000ULL,768000ULL,1536000ULL,2000000ULL,4000000ULL,8000000ULL,10000000ULL,
            16000000ULL,20000000ULL,23040000ULL,30720000ULL,40000000ULL,61440000ULL}) {
            const auto bw=static_cast<std::uint32_t>(rate/2);usb_tx_encoder valid(headers(rate,bw),1,4096,rate,bw);
            require(!valid.done(1),"rate construction");
        }
        std::cout<<"PASS bad frozen/time/reserved fields identities length channel 13 rates\n";return 0;
    } catch(const std::exception& error) {std::cerr<<"FAIL "<<error.what()<<'\n';return 1;}
}
