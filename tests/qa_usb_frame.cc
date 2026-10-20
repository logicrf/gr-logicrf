/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file qa_usb_frame.cc
 * @brief Check complete-frame commits, arbitrary fragments and isolation of configuration/sequence/time faults.
 * @details Independently constructed dual native frames, no USB; Release still checks every assertion.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.3.0
 * @date 2026-10-16
 * @note Offline protocol checks do not qualify boards or SDK cancellation.
 ******************************************************************************/
#include "usb_frame.h"
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <unistd.h>
#include <vector>
static void check(bool b,const char* why) {if(!b) throw std::runtime_error(why);}
static void put(unsigned char* p,std::uint64_t value,unsigned count) {for(unsigned i=0;i<count;++i) p[i]=static_cast<unsigned char>(value>>(8*i));}
struct capture {unsigned frames=0;std::uint64_t samples=0;bool reject=false;};
static int commit(void* raw,const unsigned char* data,size_t bytes,const usb_frame_metadata* m)
{
    auto& c=*static_cast<capture*>(raw);check(bytes==8192 && m->samples==2048,"payload geometry");
    check(m->first==m->sequence*2048ULL && m->time_valid && m->time_epoch==29,"metadata");
    for(size_t n=0;n<bytes;++n) check(data[n]==static_cast<unsigned char>(n),"segmented payload corruption");
    if(c.reject) return 1;
    ++c.frames;c.samples+=m->samples;return 0;
}
static std::vector<unsigned char> frame(unsigned ch,unsigned seq=0)
{
    std::vector<unsigned char> b(128+8192);auto* h=b.data();std::memcpy(h,"H102",4);h[4]=3;h[5]=1;h[6]=ch;h[7]=1;
    put(h+8,128,2);h[10]=seq==0;h[13]=h[14]=h[15]=h[110]=h[111]=1;
    put(h+16,8192,4);put(h+20,seq,4);put(h+24,seq*2048ULL,8);put(h+32,43,8);put(h+40,29,4);put(h+44,29,4);
    put(h+48,8000000,8);put(h+56,1,4);put(h+60,2048,4);put(h+64,2400000000ULL,8);put(h+72,4000000,4);
    put(h+80,1792100000,8);put(h+88,seq*4096,4);put(h+92,16000000,4);put(h+96,1234,8);
    h[104]=2;h[105]=h[106]=h[107]=h[109]=1;
    for(size_t n=128;n<b.size();++n) b[n]=static_cast<unsigned char>(n-128);
    return b;
}
int main()
{
    const char* project=std::getenv("LOGICRF_PROJECT_ROOT");
    if(!project) {std::cerr<<"LOGICRF_PROJECT_ROOT required\n";return 2;}
    std::filesystem::path parent=std::filesystem::path(project)/"tmp";
    std::filesystem::create_directories(parent);
    auto pattern=(parent/"usb-frame-XXXXXX").string();
    std::vector<char> name_bytes(pattern.begin(),pattern.end());name_bytes.push_back('\0');
    char* name=name_bytes.data();int fd=mkstemp(name);
    try {
        check(fd>=0,"config tempfile");std::array<unsigned char,112> config{};
        for(unsigned ch=0;ch<2;++ch) {auto* p=config.data()+ch*56;put(p,8000000,8);put(p+8,1,4);put(p+12,4000000,4);
            put(p+16,2400000000ULL,8);put(p+24,29,4);put(p+32,0x2f,4);put(p+36,1234,8);put(p+44,43,8);p[52]=2;}
        check(::write(fd,config.data(),config.size())==112,"config write");::close(fd);fd=-1;
        auto make=[&](capture& c){auto* d=usb_decoder_new(3,43,29,8000000,4000000,name,commit,&c);check(d,"expected config");return std::unique_ptr<usb_decoder,decltype(&usb_decoder_free)>(d,usb_decoder_free);};
        for(unsigned step:{1U,3U,127U,128U,129U,1023U,65536U}) {
            capture c;auto d=make(c);auto wire=frame(1);auto b=frame(2);wire.insert(wire.end(),b.begin(),b.end());
            b=frame(1,1);wire.insert(wire.end(),b.begin(),b.end());
            for(size_t off=0;off<wire.size();) {auto n=std::min<size_t>(step,wire.size()-off);
                check(!usb_decoder_feed(d.get(),wire.data()+off,n),"valid segmented frame rejected");off+=n;}
            check(c.frames==3 && c.samples==6144 && usb_decoder_partial(d.get())==0,"complete commit count");
        }
        std::cout<<"PASS segmentation-seven-boundaries dual-lane consecutive IQ payload\n";
        {capture c;auto d=make(c);auto b=frame(1);check(!usb_decoder_feed(d.get(),b.data(),b.size()-1),"partial feed");
         check(c.frames==0 && usb_decoder_partial(d.get())==b.size()-1,"partial leaked");}
        std::cout<<"PASS truncated payload publishes zero\n";
        struct fault {unsigned offset;const char* reason;};
        for(const auto& fault:std::vector<fault>{{112,"reserved"},{32,"stream"},{40,"configuration"},{44,"epoch"},{64,"frequency"},
            {72,"bandwidth"},{96,"device"},{20,"sequence"},{24,"sample index"},{13,"time validity"},{92,"time clock"}}) {
            capture c;auto d=make(c);auto b=frame(1);b[fault.offset]^=0x80;
            check(usb_decoder_feed(d.get(),b.data(),b.size()),fault.reason);check(c.frames==0 && usb_decoder_frames(d.get(),1)==0,"bad frame published/counted");
            auto valid=frame(1);check(usb_decoder_feed(d.get(),valid.data(),valid.size()),"error not sticky");
        }
        {capture c;auto d=make(c);auto b=frame(1);check(!usb_decoder_feed(d.get(),b.data(),b.size()),"first");
         b=frame(1,1);put(b.data()+88,4097,4);check(usb_decoder_feed(d.get(),b.data(),b.size()),"bad cadence accepted");check(c.frames==1,"cadence leaked");}
        {capture c;c.reject=true;auto d=make(c);auto b=frame(1);check(usb_decoder_feed(d.get(),b.data(),b.size()),"consumer failure lost");check(c.frames==0 && usb_decoder_samples(d.get(),1)==0,"consumer leak/count");}
        std::cout<<"PASS eleven identity/geometry/time faults sticky + cadence + consumer reject\n";
        ::unlink(name);return 0;
    } catch(const std::exception& e) {if(fd>=0) ::close(fd);::unlink(name);std::cerr<<"FAIL "<<e.what()<<'\n';return 1;}
}
