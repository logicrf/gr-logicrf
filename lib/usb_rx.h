/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_rx.h
 * @brief Managed local C++ receiver from an independent FT601 process to GNU Radio.
 * @details UNIX transports native bytes; C/C++ validates/converts frames without Python IQ relay.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.3.0
 * @date 2026-10-16
 * @note Socket permits same-UID connections only; outer code retains device locks and independent post.
 ******************************************************************************/
#pragma once
#include "usb_frame.h"
#include <gnuradio/logicrf/device.h>
#include <atomic>
#include <complex>
#include <functional>
#include <mutex>
#include <vector>
namespace gr::logicrf {
/** @brief One worker shares both channels; stop control runs separately and draining continues receiving bytes. */
class usb_rx {
public:
    struct hooks {
        std::function<bool()> permitted, stopping;
        std::function<void(const std::string&)> guard, fail;
        std::function<void(unsigned,std::vector<std::complex<float>>,const usb_frame_metadata&)> deliver;
        std::function<void()> ready;
        std::function<void(std::uint64_t)> partial;
    };
    usb_rx(device_options options,unsigned mask,hooks callbacks);
    ~usb_rx();
    void run() noexcept;
    /** @brief Normal STOP obtains board STOP/ACK before helper callbacks quiesce/drain and close the socket. */
    void finish_window();
    /** @brief Wake the local socket only; never release SDK objects; outer finish/post still recovers errors. */
    void cancel() noexcept;
private:
    device_options options;
    unsigned mask;
    hooks callbacks;
    std::atomic<int> fd{-1}, listener{-1};
    std::mutex socket_mutex; ///< Serialize cancel/close so a reused fd cannot be shut down accidentally.
    std::atomic<bool> window{false}, cancelled{false};
};
}
