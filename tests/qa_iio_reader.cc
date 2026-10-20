/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file qa_iio_reader.cc
 * @brief Check actual libiio receiver chunking/rejection through a fixed local IIOD endpoint.
 * @details No board connection; retain cross-block RAMP checks and actual JSON output/guard cleanup.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.1
 * @date 2026-10-16
 * @note This fixture verifies protocol/lifecycle, not hardware losslessness.
 ******************************************************************************/
#include "iio_rx.h"
#include <atomic>
#include <cmath>
#include <iostream>
#include <string>
using gr::logicrf::iio_rx;
int main()
{
    std::atomic<bool> stopped{false};
    std::string error;
    std::uint64_t count=0, bad=0;
    unsigned begin=0,arm=0,end=0;
    bool previous_valid=false;
    std::uint16_t previous=0;
    iio_rx::hooks hook;
    hook.permitted=[] { return true; };
    hook.stopping=[&] { return stopped.load(); };
    hook.fail=[&](const std::string& reason) { error=reason;stopped=true; };
    hook.ready=[] {};
    hook.guard=[&](const std::string& stage) {
        if(stage=="begin")++begin;
        else if(stage=="arm")++arm;
        else if(stage=="end")++end;
    };
    hook.deliver=[&](iio_rx::sample_block values) {
        for(const auto& v:values) {
            const auto i=static_cast<std::uint16_t>(std::lround(v.real()*32768));
            const auto q=static_cast<std::uint16_t>(std::lround(v.imag()*32768));
            if(!previous_valid && i!=544)++bad;
            if(i!=q || (previous_valid && static_cast<std::uint16_t>(i-previous)!=1))++bad;
            previous=i;previous_valid=true;++count;
        }
    };
    // Reject CLI URIs; fake permission callbacks can reach only the fixed loopback simulator.
    iio_rx reader("ip:127.0.0.102",1,16,37,std::move(hook));
    reader.run();
    std::cout<<"{\"samples\":"<<count<<",\"ramp_errors\":"<<bad
             <<",\"begin\":"<<begin<<",\"arm\":"<<arm<<",\"end\":"<<end
             <<",\"error\":\""<<error<<"\"}\n";
    return 0;
}
