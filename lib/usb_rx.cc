/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_rx.cc
 * @brief Receive FT601 helper bytes and publish IQ only after full configuration/time validation.
 * @details One UNIX socket serves both channels; callbacks enqueue Device frames without per-block Python calls.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.3.0
 * @date 2026-10-16
 * @note Peer exit is not success evidence; outer code must verify independent native main/post.
 ******************************************************************************/
#include "usb_rx.h"
#include <array>
#include <chrono>
#include <cstring>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
namespace gr::logicrf {
usb_rx::usb_rx(device_options o,unsigned mask,hooks h):options(std::move(o)),mask(mask),callbacks(std::move(h)) {}
usb_rx::~usb_rx() {cancel();}
void usb_rx::cancel() noexcept
{
    cancelled=true;
    std::lock_guard<std::mutex> lock(socket_mutex);
    const int socket=fd.load();if(socket>=0) ::shutdown(socket,SHUT_RDWR);
}
void usb_rx::finish_window() {if(window.exchange(false)) callbacks.guard("usb-stop");}
void usb_rx::run() noexcept
{
    usb_decoder* decoder=nullptr;
    std::string consumer_error;
    struct context_type {usb_rx* self;std::string* error;} context{this,&consumer_error};
    auto deliver=[](void* raw,const unsigned char* data,size_t bytes,const usb_frame_metadata* m)->int {
        auto& c=*static_cast<context_type*>(raw);
        try {
            std::vector<std::complex<float>> samples(bytes/4);
            for(size_t n=0;n<samples.size();++n) {
                const auto i=static_cast<std::int16_t>(std::uint16_t(data[4*n])|(std::uint16_t(data[4*n+1])<<8));
                const auto q=static_cast<std::int16_t>(std::uint16_t(data[4*n+2])|(std::uint16_t(data[4*n+3])<<8));
                samples[n]={float(i)/32768.0f,float(q)/32768.0f};
            }
            c.self->callbacks.deliver(m->channel,std::move(samples),*m);return 0;
        } catch(const std::exception& e) {*c.error=e.what();return 1;}
    };
    try {
        decoder=usb_decoder_new(mask,options.usb_stream_id,options.usb_time_epoch,
            static_cast<std::uint64_t>(options.sample_rate),static_cast<std::uint32_t>(options.rf_bandwidth),
            options.usb_configuration.c_str(),deliver,&context);
        if(!decoder) throw std::runtime_error("USB_EXPECTED_CONFIGURATION_INVALID");
        sockaddr_un address{};address.sun_family=AF_UNIX;
        if(options.usb_socket.size()>=sizeof(address.sun_path)) throw std::runtime_error("USB_SOCKET_PATH_TOO_LONG");
        std::memcpy(address.sun_path,options.usb_socket.c_str(),options.usb_socket.size()+1);
        int server=::socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);listener=server;
        if(server<0 || ::bind(server,reinterpret_cast<sockaddr*>(&address),sizeof(address)) ||
            ::chmod(options.usb_socket.c_str(),0600) || ::listen(server,1)) throw std::runtime_error("USB_SOCKET_LISTEN");
        auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(15);
        while(true) {
            if(cancelled || !callbacks.permitted()) throw std::runtime_error("USB_ACCEPT_PERMISSION");
            pollfd p{server,POLLIN,0};int r=::poll(&p,1,50);
            if(r>0) break;
            if(r<0 || std::chrono::steady_clock::now()>deadline) throw std::runtime_error("USB_HELPER_CONNECT_TIMEOUT");
        }
        int socket=::accept4(server,nullptr,nullptr,SOCK_CLOEXEC);
        {std::lock_guard<std::mutex> lock(socket_mutex);fd=socket;}
        ::close(listener.exchange(-1));
        ucred peer{};socklen_t length=sizeof(peer);
        if(socket<0 || ::getsockopt(socket,SOL_SOCKET,SO_PEERCRED,&peer,&length) || peer.uid!=::getuid())
            throw std::runtime_error("USB_HELPER_PEER_UID");
        window=true;callbacks.guard("usb-start");callbacks.ready();
        std::array<unsigned char,65536> block;
        auto last=std::chrono::steady_clock::now();
        while(!cancelled) {
            if(!callbacks.permitted()) throw std::runtime_error("USB_LOCAL_PERMISSION_EXPIRED");
            pollfd p{socket,POLLIN,0};int r=::poll(&p,1,50);
            if(r<0) throw std::runtime_error("USB_SOCKET_POLL");
            if(!r) {
                if(std::chrono::steady_clock::now()-last>std::chrono::seconds(5)) throw std::runtime_error("USB_DATA_DEADLINE");
                continue;
            }
            auto n=::recv(socket,block.data(),block.size(),0);
            if(n==0) break;
            if(n<0) {if(cancelled) break;throw std::runtime_error("USB_SOCKET_READ");}
            last=std::chrono::steady_clock::now();
            if(const char* e=usb_decoder_feed(decoder,block.data(),static_cast<size_t>(n)))
                throw std::runtime_error(consumer_error.empty()?std::string("USB_V3:")+e:consumer_error);
        }
        callbacks.partial(usb_decoder_partial(decoder));
        for(unsigned ch=1;ch<=2;++ch) if((mask&(1U<<(ch-1))) && !usb_decoder_frames(decoder,ch))
            throw std::runtime_error("USB_MISSING_CHANNEL");
    } catch(const std::exception& e) {if(!cancelled) callbacks.fail(e.what());}
    window=false;
    {std::lock_guard<std::mutex> lock(socket_mutex);
     int socket=fd.exchange(-1);if(socket>=0) ::close(socket);}
    int socket;
    socket=listener.exchange(-1);if(socket>=0) ::close(socket);
    ::unlink(options.usb_socket.c_str());usb_decoder_free(decoder);
}
}
