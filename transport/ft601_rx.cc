/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file ft601_rx.cc
 * @brief Independent unprivileged FT601 RX process sends native bytes over bounded local sockets.
 * @details One handle, 255 slots, event-synchronized completion before resubmission; independent writer prevents downstream jitter starving USB.
 *          Root retains original locks/quotas/bounded service; ACK follows STOP proof; EOS follows callback quiescence and submitted-byte drain.
 *          EOS precedes final SDK release; host main/post must actually pass before publishing completed recordings.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.4.0
 * @date 2026-10-18
 * @note No board/RF operations; failed cancellation exits the process; outer post remains mandatory.
 ******************************************************************************/
extern "C" {
#include "h102_ft601_completion.h"
#include "h102_ft601_persistent_dma.h"
}
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <csignal>
#include <memory>
#include <string>
#include <deque>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <thread>
#include <vector>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
namespace {
constexpr unsigned depth=255,block=65536,ring_slots=2048;
struct slot {OVERLAPPED op{};std::vector<unsigned char> data;bool initialized=false,pending=false;};
double now() {return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
/** @brief Fixed 128 MiB limit; slow consumers fail explicitly without dropping old data. */
class wire_queue {
public:
    explicit wire_queue(int socket):socket(socket),storage(std::size_t(ring_slots)*block),lengths(ring_slots) {
        for(std::size_t p=0;p<storage.size();p+=4096) storage[p]=0;
        writer=std::thread([this]{run();});
    }
    ~wire_queue() {finish();}
    void push(const unsigned char* data,std::size_t bytes) {
        std::lock_guard<std::mutex> lock(mutex);
        if(failed || used==ring_slots) throw std::runtime_error("USB_WIRE_QUEUE_OVERFLOW");
        std::memcpy(storage.data()+std::size_t(tail)*block,data,bytes);lengths[tail]=bytes;
        tail=(tail+1)%ring_slots;++used;peak=std::max(peak,used);changed.notify_one();
    }
    void finish() { {std::lock_guard<std::mutex> lock(mutex);done=true;changed.notify_all();} if(writer.joinable()) writer.join(); }
    std::atomic<bool> failed{false};std::uint64_t sent=0;unsigned peak=0;
private:
    void run() {
        for(;;) {
            unsigned slot;std::size_t bytes;
            {std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[this]{return done||used;});
             if(!used) break;
             slot=head;bytes=lengths[slot];}
            const auto* data=storage.data()+std::size_t(slot)*block;
            std::size_t offset=0;auto deadline=now()+3;
            while(offset<bytes) {
                pollfd p{socket,POLLOUT,0};int r=::poll(&p,1,20);
                if(r<0 || (r>0 && p.revents&(POLLERR|POLLHUP|POLLNVAL)) || now()>deadline) {failed=true;break;}
                if(!r) continue;
                auto n=::send(socket,data+offset,bytes-offset,MSG_NOSIGNAL|MSG_DONTWAIT);
                if(n<0) {if(errno==EAGAIN || errno==EINTR) continue;failed=true;break;}
                if(!n) {failed=true;break;}offset+=static_cast<std::size_t>(n);
            }
            if(failed) break;
            sent+=bytes;
            {std::lock_guard<std::mutex> lock(mutex);head=(head+1)%ring_slots;--used;}
        }
        ::shutdown(socket,SHUT_WR);
    }
    int socket;std::vector<unsigned char> storage;std::vector<std::size_t> lengths;
    unsigned head=0,tail=0,used=0;bool done=false;
    std::mutex mutex;std::condition_variable changed;std::thread writer;
};
}
int main(int argc,char** argv)
{
    if(argc!=5 || ::getuid()==0) {std::fprintf(stderr,"usage: ft601_rx SERIAL SOCKET SECONDS RATE (ordinary user)\n");return 2;}
    std::setvbuf(stdout,nullptr,_IOLBF,0);std::signal(SIGPIPE,SIG_IGN);
    FT_HANDLE handle=nullptr;std::array<slot,depth> slots;
    std::unique_ptr<wire_queue> queue;int socket=-1;bool streaming=false,ack=false,ready=false;
    std::string error;std::uint64_t bytes=0,calls=0,cancelled_bytes=0;unsigned index=0,cancelled_slots=0,unknown_slots=0;
    try {
        unsigned seconds=std::stoul(argv[3]);std::uint64_t rate=std::stoull(argv[4]);
        if(seconds>300 || rate<192000 || rate>61440000) throw std::runtime_error("USB_ARGUMENT_RANGE");
        const double opening_at=now();
        socket=::socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);sockaddr_un address{};address.sun_family=AF_UNIX;
        if(std::strlen(argv[2])>=sizeof(address.sun_path)) throw std::runtime_error("USB_SOCKET_PATH");
        std::strcpy(address.sun_path,argv[2]);double connect_deadline=now()+15;
        while(::connect(socket,reinterpret_cast<sockaddr*>(&address),sizeof(address))) {
            if(now()>connect_deadline) throw std::runtime_error("USB_PARENT_CONNECT");
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        queue=std::make_unique<wire_queue>(socket);
        const double connected_at=now();
        if(h102_dma_initialize()) throw std::runtime_error("USB_SDK_LIBUSB_PIN");
        const double initialized_at=now();
        FT_TRANSFER_CONF conf{};conf.wStructSize=sizeof(conf);
        for(unsigned i=0;i<2;++i) {conf.pipe[i].bURBCount=depth;conf.pipe[i].dwURBBufferSize=32768;}
        if(FT_SetTransferParams(&conf,0)!=FT_OK || FT_Create(argv[1],FT_OPEN_BY_SERIAL_NUMBER,&handle)!=FT_OK)
            throw std::runtime_error("USB_OPEN");
        const double opened_at=now();
        FT_60XCONFIGURATION chip{};
        if(FT_GetChipConfiguration(handle,&chip)!=FT_OK || chip.VendorID!=0x0403 || chip.ProductID!=0x601f ||
            chip.FIFOClock!=0 || chip.FIFOMode!=0 || chip.ChannelConfig!=2) throw std::runtime_error("USB_CHIP_CONFIGURATION");
        // Fresh handle: no host transfer needs cancellation, but the device
        // FIFO must be reset before READY. The pinned SDK's FlushPipe issues
        // that device command without AbortPipe's unconditional 500 ms wait.
        // Its return alone is insufficient: require a real empty read below
        // and exact native framing/epoch/stream validation during reception.
        if(FT_FlushPipe(handle,0x82)!=FT_OK) throw std::runtime_error("USB_INITIAL_FLUSH");
        // Empty is established by a real zero-byte read, not a sleep. Keep
        // draining every nonempty result; stale bytes arriving afterwards are
        // rejected by the receiver's exact epoch/stream/header validator.
        unsigned char drain[1024];bool empty=false;
        for(unsigned n=0;n<256;++n) {ULONG got=0;auto status=FT_ReadPipeEx(handle,0,drain,sizeof(drain),&got,10);
            if(got>sizeof(drain) || (status!=FT_OK && status!=FT_TIMEOUT)) break;
            if(!got) {empty=true;break;}}
        if(!empty) throw std::runtime_error("USB_DRAIN");
        const double drained_at=now();
        // At the lowest rate, 255 slots can validly hold over one second; deadlines cover READY and all in-flight data.
        DWORD timeout=static_cast<DWORD>(15000+(std::uint64_t(depth)*block*1000/(4*rate)));
        if(FT_SetPipeTimeout(handle,0x82,timeout)!=FT_OK) throw std::runtime_error("USB_TIMEOUT");
        for(auto& s:slots) {s.data.resize(block);if(FT_InitializeOverlapped(handle,&s.op)!=FT_OK) throw std::runtime_error("USB_EVENT");s.initialized=true;}
        const double events_at=now();
        if(FT_SetStreamPipe(handle,FALSE,FALSE,0x82,block)!=FT_OK) throw std::runtime_error("USB_STREAM");
        streaming=true;
        auto submit=[&](slot& s) {ULONG got=0;auto status=FT_ReadPipeAsync(handle,0,s.data.data(),block,&got,&s.op);
            if(status!=FT_OK && status!=FT_IO_PENDING) throw std::runtime_error("USB_SUBMIT");
            s.pending=true;};
        for(auto& s:slots) submit(s);
        // Monotonic diagnostics distinguish SDK open/setup from the actual
        // ready barrier; no phase below constitutes permission to start RF.
        std::printf("START_PHASE connect_queue_seconds=%.6f initialize_seconds=%.6f open_seconds=%.6f drain_seconds=%.6f events_seconds=%.6f stream_submit_seconds=%.6f\n",
            connected_at-opening_at,initialized_at-connected_at,opened_at-initialized_at,
            drained_at-opened_at,events_at-drained_at,now()-events_at);
        ready=true;std::printf("READY\n");double ready_at=now(),first=0,last=0,next_control=0,stop_at=0;
        for(;;) {
            double t=now();
            if(t>=next_control) {
                next_control=t+.001;pollfd p{STDIN_FILENO,POLLIN,0};int r=::poll(&p,1,0);
                if(r>0) {char c=0;if(!(p.revents&POLLIN) || ::read(STDIN_FILENO,&c,1)!=1) throw std::runtime_error("USB_CONTROLLER_LOST");
                    if(c=='\n' && stop_at) {ack=true;break;}
                    if(c=='S' || c=='C') {if(!stop_at) {stop_at=t;std::printf("STOP_REQUEST\n");}}
                    else if(c!='\n') throw std::runtime_error("USB_CONTROL_COMMAND");}
            }
            if(seconds && !stop_at && first && t-first>=seconds) {stop_at=t;std::printf("STOP_REQUEST\n");}
            if((stop_at && t-stop_at>5) || (!first && t-ready_at>15) || (first && !stop_at && t-last>3.5))
                throw std::runtime_error("USB_PROGRESS_OR_STOP_DEADLINE");
            if(queue->failed) throw std::runtime_error("USB_WRITER_FAILED");
            auto& s=slots[index];ULONG got=0;auto status=completed_read(handle,&s.op,&got);
            if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING) {std::this_thread::sleep_for(std::chrono::microseconds(25));continue;}
            if(status!=FT_OTHER_ERROR) s.pending=false;
            ++calls;if(status!=FT_OK || got>block) throw std::runtime_error("USB_COMPLETION");
            if(got) {if(!first) first=t;last=t;queue->push(s.data.data(),got);bytes+=got;}
            submit(s);index=(index+1)%depth;
        }
    } catch(const std::exception& e) {error=e.what();std::printf("ERROR %s\n",error.c_str());}
    if(ready && !ack) {std::printf("STOP_REQUEST\n");pollfd p{STDIN_FILENO,POLLIN,0};char c=0;
        ack=::poll(&p,1,5000)>0 && ::read(STDIN_FILENO,&c,1)==1 && c=='\n';}
    bool cleanup=true;
    double quiesce_seconds=0,wire_finish_seconds=0,sdk_release_seconds=0;
    if(handle) {
        // Never Close while SDK owns callbacks/memory; failed quiescence relies on process exit and outer post.
        const double quiesce_at=now();
        if(h102_dma_quiesce(2000)) {std::printf("UNQUIESCED\n");std::fflush(stdout);::_exit(74);}
        quiesce_seconds=now()-quiesce_at;
        for(auto& s:slots) if(s.pending) {
            ULONG count=0;const auto status=completed_read(handle,&s.op,&count);
            if(status==FT_IO_PENDING || status==FT_IO_INCOMPLETE || status==FT_OTHER_ERROR) ++unknown_slots;
            else {++cancelled_slots;cancelled_bytes+=count;s.pending=false;}
        }
        // Production loop ended and DMA callbacks quiesced; only bytes committed to wire_queue belong to this window.
        // finish sends those bytes before SHUT_WR; unpublished cancelled tails retain original accounting.
        // SDK release failures propagate through helper results/independent post; EOS alone proves neither success nor release.
        if(queue && ready && ack && error.empty()) {
            const double wire_at=now();queue->finish();wire_finish_seconds=now()-wire_at;
        }
        const double release_at=now();
        if(streaming) {
            // h102_dma_quiesce has already cancelled every actual transfer and
            // waited for callbacks. Only a normal STOP/ACK with known terminal
            // slots can omit the SDK's redundant fixed-delay cancellation.
            // Error/unknown-tail paths retain the original abort procedure.
            if(!ready || !ack || !error.empty() || unknown_slots)
                cleanup=FT_AbortPipe(handle,0x82)==FT_OK && cleanup;
            cleanup=FT_ClearStreamPipe(handle,FALSE,FALSE,0x82)==FT_OK && cleanup;
        }
        for(auto& s:slots) if(s.initialized) cleanup=FT_ReleaseOverlapped(handle,&s.op)==FT_OK && cleanup;
        cleanup=FT_Close(handle)==FT_OK && cleanup;
        sdk_release_seconds=now()-release_at;
    }
    if(queue) queue->finish();
    std::printf("STOP_PHASE quiesce_seconds=%.6f wire_finish_seconds=%.6f sdk_release_seconds=%.6f\n",
        quiesce_seconds,wire_finish_seconds,sdk_release_seconds);
    std::printf("STOP_TAIL cancelled_queue_bytes=%llu cancelled_slots=%u unknown_slots=%u\n",
        (unsigned long long)cancelled_bytes,cancelled_slots,unknown_slots);
    std::printf("RESULT bytes=%llu sent=%llu calls=%llu peak=%u ack=%u cleanup=%u error=%s\n",
        (unsigned long long)bytes,(unsigned long long)(queue?queue->sent:0),(unsigned long long)calls,
        queue?queue->peak:0,ack,cleanup,error.empty()?"none":error.c_str());
    if(socket>=0) ::close(socket);
    return !error.empty() || !ready || !ack || !cleanup || (queue && queue->failed);
}
