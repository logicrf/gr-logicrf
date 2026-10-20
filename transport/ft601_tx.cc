/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file ft601_tx.cc
 * @brief Unprivileged finite FT601 TX with IQ exclusively from GNU Radio local sockets.
 * @details Independent bounded input worker; 128-request completion loop never waits for IQ. DRAIN/A/FLUSHED/Q,
 *          READY/S and STOP_REQUEST/ACK are separate checks; free SDK memory only after callbacks quiesce.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.5.0
 * @date 2026-10-16
 * @note USB completion is not SSI consumption; independent board ownership verifies RF/digital templates.
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
#include <deque>
#include <memory>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace {
constexpr unsigned depth=128,block=8320*8,ring_slots=512;
struct slot {OVERLAPPED op{};std::vector<unsigned char> data;bool initialized=false,pending=false;};
double now() {return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
/** @brief Exact integer CLI parsing rejects negative signs, overflow and trailing text. */
std::uint64_t number(const char* text)
{
    if(!*text || *text<'0' || *text>'9') throw std::invalid_argument("TX_ARGUMENT_NUMBER");
    std::size_t used=0;auto value=std::stoull(text,&used);
    if(used!=std::strlen(text)) throw std::invalid_argument("TX_ARGUMENT_SUFFIX");
    return value;
}
/** @brief Await root-verified one-byte proof; EOF/timeout cannot advance stages. */
void control(char expected,double seconds)
{
    auto deadline=now()+seconds;
    while(now()<deadline) {
        pollfd p{STDIN_FILENO,POLLIN,0};auto r=::poll(&p,1,20);
        if(r<0) {if(errno==EINTR) continue;throw std::runtime_error("TX_CONTROL_POLL");}
        if(!r) continue;
        char c=0;
        if(!(p.revents&POLLIN) || ::read(STDIN_FILENO,&c,1)!=1) throw std::runtime_error("TX_CONTROLLER_EOF");
        if(c!=expected) throw std::runtime_error("TX_CONTROL_PHASE");
        return;
    }
    throw std::runtime_error("TX_CONTROL_DEADLINE");
}
/** @brief Socket reads fill a fixed 34 MiB ring in whole blocks; main USB loop uses nonblocking pop only. */
class input_queue {
public:
    input_queue(int fd,std::uint64_t expected):fd(fd),expected(expected),storage(std::size_t(ring_slots)*block) {
        reader=std::thread([this]{run();});
    }
    ~input_queue() {cancel();}
    bool pop(unsigned char* output) {
        std::lock_guard<std::mutex> lock(mutex);
        if(!used) return false;
        std::memcpy(output,storage.data()+std::size_t(head)*block,block);
        head=(head+1)%ring_slots;--used;changed.notify_all();return true;
    }
    void cancel() {
        stopping=true;::shutdown(fd,SHUT_RD);changed.notify_all();
        if(reader.joinable()) reader.join();
    }
    std::atomic<bool> failed{false},finished{false};
    std::atomic<std::uint64_t> received{0};
    unsigned peak=0;
private:
    void run() {
        try {
            while((!expected || received<expected) && !stopping) {
                unsigned index;
                {std::unique_lock<std::mutex> lock(mutex);
                 if(!changed.wait_for(lock,std::chrono::seconds(3),[this]{return stopping||used<ring_slots;}))
                     throw std::runtime_error("TX_INPUT_QUEUE_DEADLINE");
                 if(stopping) return;
                 index=tail;}
                auto* output=storage.data()+std::size_t(index)*block;
                std::size_t offset=0;auto deadline=now()+5;
                while(offset<block && !stopping) {
                    pollfd p{fd,POLLIN,0};auto ready=::poll(&p,1,20);
                    if(ready<0 && errno==EINTR) continue;
                    if(ready<0 || now()>deadline) throw std::runtime_error("TX_INPUT_PROGRESS");
                    if(!ready) continue;
                    auto n=::recv(fd,output+offset,block-offset,MSG_DONTWAIT);
                    if(n<0 && (errno==EAGAIN||errno==EINTR)) continue;
                    if(n==0 && !expected && offset==0) {finished=true;return;}
                    if(n<=0) throw std::runtime_error("TX_INPUT_TRUNCATED");
                    offset+=n;
                }
                if(stopping) return;
                received+=block;
                {std::lock_guard<std::mutex> lock(mutex);tail=(tail+1)%ring_slots;++used;peak=std::max(peak,used);}
            }
            // Finite input must end with actual EOF; never swallow surplus caller bytes.
            auto deadline=now()+5;
            while(!stopping) {
                pollfd p{fd,POLLIN,0};auto r=::poll(&p,1,20);
                if(r<0 && errno==EINTR) continue;
                if(r<0 || now()>deadline) throw std::runtime_error("TX_INPUT_EOF_DEADLINE");
                if(!r) continue;
                unsigned char extra;
                auto n=::recv(fd,&extra,1,MSG_DONTWAIT);
                if(n<0 && (errno==EAGAIN||errno==EINTR)) continue;
                if(n!=0) throw std::runtime_error("TX_INPUT_AFTER_FINITE_END");
                finished=true;return;
            }
        } catch(...) {if(!stopping) failed=true;}
    }
    int fd;std::uint64_t expected;std::vector<unsigned char> storage;
    unsigned head=0,tail=0,used=0;
    std::mutex mutex;std::condition_variable changed;std::thread reader;
    std::atomic<bool> stopping{false};
};
}

int main(int argc,char** argv)
{
    if(argc!=8 || ::getuid()==0) {std::fprintf(stderr,"usage: ft601_tx SERIAL SOCKET SECONDS RATE MASK SAMPLES PARENT (ordinary user)\n");return 2;}
    std::setvbuf(stdout,nullptr,_IOLBF,0);std::signal(SIGPIPE,SIG_IGN);
    FT_HANDLE handle=nullptr;std::array<slot,depth> slots;
    std::unique_ptr<input_queue> queue;int socket=-1;
    bool streaming=false,ack=false,ready=false,started=false,cancelled=false,cleanup=true;
    std::string error;std::uint64_t submitted=0,completed=0,expected=0,calls=0,cancelled_bytes=0;
    unsigned unknown=0;
    try {
        auto seconds=number(argv[3]),rate=number(argv[4]),mask=number(argv[5]),samples=number(argv[6]),parent=number(argv[7]);
        if(std::strcmp(argv[1],"000000000001") || seconds>300 || rate<192000 || rate>61440000 ||
            mask<1 || mask>3 || (samples && samples<16384) || samples%16384 || ((seconds==0)!=(samples==0)) || (samples && samples>rate*(seconds+1)) || parent<=1 || parent>2147483647)
            throw std::runtime_error("TX_ARGUMENT_RANGE");
        if(prctl(PR_SET_PDEATHSIG,SIGKILL)!=0 || getppid()!=static_cast<pid_t>(parent))
            throw std::runtime_error("TX_CONTROLLER_PARENT_CHANGED");
        expected=samples/2048*8320*(mask==3?2:1);
        socket=::socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);sockaddr_un address{};address.sun_family=AF_UNIX;
        if(socket<0 || std::strlen(argv[2])>=sizeof(address.sun_path)) throw std::runtime_error("TX_SOCKET_PATH");
        std::strcpy(address.sun_path,argv[2]);auto connect_deadline=now()+15;
        while(::connect(socket,reinterpret_cast<sockaddr*>(&address),sizeof(address))) {
            if(now()>connect_deadline) throw std::runtime_error("TX_SOCKET_CONNECT");
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if(h102_dma_initialize()) throw std::runtime_error("TX_SDK_LIBUSB_PIN");
        FT_TRANSFER_CONF conf{};conf.wStructSize=sizeof(conf);
        for(unsigned i=0;i<2;++i) {conf.pipe[i].bURBCount=depth;conf.pipe[i].dwURBBufferSize=32768;}
        if(FT_SetTransferParams(&conf,0)!=FT_OK || FT_Create(argv[1],FT_OPEN_BY_SERIAL_NUMBER,&handle)!=FT_OK)
            throw std::runtime_error("TX_USB_OPEN");
        FT_60XCONFIGURATION chip{};
        if(FT_GetChipConfiguration(handle,&chip)!=FT_OK || chip.VendorID!=0x0403 || chip.ProductID!=0x601f ||
            chip.FIFOClock!=0 || chip.FIFOMode!=0 || chip.ChannelConfig!=2) throw std::runtime_error("TX_USB_CHIP");
        std::printf("DRAIN_REQUEST\n");control('A',8);
        if(FT_AbortPipe(handle,0x02)!=FT_OK) throw std::runtime_error("TX_INITIAL_ABORT");
        std::printf("FLUSHED\n");control('Q',5);
        if(FT_SetPipeTimeout(handle,0x02,15000+depth*block*1000ULL/(4*rate))!=FT_OK)
            throw std::runtime_error("TX_USB_TIMEOUT");
        std::deque<unsigned> free,pending;
        for(unsigned i=0;i<depth;++i) {
            auto& s=slots[i];s.data.resize(block);
            if(FT_InitializeOverlapped(handle,&s.op)!=FT_OK) throw std::runtime_error("TX_USB_EVENT");
            s.initialized=true;free.push_back(i);
        }
        if(FT_SetStreamPipe(handle,FALSE,FALSE,0x02,block)!=FT_OK) throw std::runtime_error("TX_USB_STREAM");
        streaming=true;queue=std::make_unique<input_queue>(socket,expected);
        auto beginning=now(),last_progress=beginning;
        for(;;) {
            pollfd p{STDIN_FILENO,POLLIN,0};auto event=::poll(&p,1,0);
            if(event<0 && errno!=EINTR) throw std::runtime_error("TX_CONTROL_POLL");
            if(event>0) {
                char c=0;if(!(p.revents&POLLIN) || ::read(STDIN_FILENO,&c,1)!=1) throw std::runtime_error("TX_CONTROLLER_LOST");
                if(c=='C') {cancelled=true;break;}
                if(c=='S' && ready && !started) started=true;
                else throw std::runtime_error("TX_CONTROL_RUNNING_PHASE");
            }
            if(queue->failed) throw std::runtime_error("TX_INPUT_FAILED");
            for(unsigned count=0;count<32 && !free.empty() && (!expected || submitted<expected);++count) {
                auto index=free.front();auto& s=slots[index];
                if(!queue->pop(s.data.data())) break;
                ULONG got=0;s.op.Internal=0;s.op.InternalHigh=0;
                auto status=FT_WritePipeAsync(handle,0,s.data.data(),block,&got,&s.op);
                if(status!=FT_OK && status!=FT_IO_PENDING) throw std::runtime_error("TX_USB_SUBMIT");
                s.pending=true;pending.push_back(index);free.pop_front();submitted+=block;
            }
            if(!ready && submitted>=(expected?std::min<std::uint64_t>(expected,2*block):2*block)) {ready=true;std::printf("READY\n");}
            if(!pending.empty()) {
                auto index=pending.front();auto& s=slots[index];ULONG got=0;
                auto status=completed_read(handle,&s.op,&got);++calls;
                if(status!=FT_IO_INCOMPLETE && status!=FT_IO_PENDING) {
                    if(status!=FT_OTHER_ERROR) s.pending=false;
                    if(status!=FT_OK || got!=block) throw std::runtime_error("TX_USB_WRITE_COMPLETION");
                    completed+=got;last_progress=now();pending.pop_front();free.push_back(index);
                }
            }
            if(started && queue->finished && completed==queue->received && (!expected || completed==expected)) break;
            if((seconds && now()-beginning>seconds+20) || now()-last_progress>15) throw std::runtime_error("TX_USB_PROGRESS_DEADLINE");
            std::this_thread::sleep_for(std::chrono::microseconds(25));
        }
    } catch(const std::exception& e) {error=e.what();std::printf("ERROR %s\n",error.c_str());}
    std::printf("%s\nSTOP_REQUEST\n",cancelled?"CANCELLED":error.empty()?"USB_DONE":"USB_FAILED");
    if(handle) {
        try {control('\n',8);ack=true;} catch(const std::exception& e) {if(error.empty()) error=e.what();}
        if(!ack) {
            std::printf("RESULT submitted=%llu completed=%llu expected=%llu ack=0 cleanup=0 error=%s\n",
                (unsigned long long)submitted,(unsigned long long)completed,(unsigned long long)expected,error.c_str());
            std::fflush(stdout);::_exit(75);
        }
        if(queue) queue->cancel();
        if(h102_dma_quiesce(2000)) {std::printf("UNQUIESCED\n");std::fflush(stdout);::_exit(74);}
        for(auto& s:slots) if(s.pending) {
            ULONG got=0;auto status=completed_read(handle,&s.op,&got);
            if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING || status==FT_OTHER_ERROR) ++unknown;
            else {s.pending=false;cancelled_bytes+=got;}
        }
        if(unknown) {std::printf("UNKNOWN_CALLBACKS %u\n",unknown);std::fflush(stdout);::_exit(74);}
        if(streaming) {cleanup=FT_AbortPipe(handle,0x02)==FT_OK && cleanup;cleanup=FT_ClearStreamPipe(handle,FALSE,FALSE,0x02)==FT_OK && cleanup;}
        for(auto& s:slots) if(s.initialized) cleanup=FT_ReleaseOverlapped(handle,&s.op)==FT_OK && cleanup;
        cleanup=FT_Close(handle)==FT_OK && cleanup;
    }
    std::printf("RESULT submitted=%llu completed=%llu expected=%llu received=%llu cancelled_bytes=%llu calls=%llu ack=%u cleanup=%u cancelled=%u error=%s\n",
        (unsigned long long)submitted,(unsigned long long)completed,(unsigned long long)expected,
        (unsigned long long)(queue?queue->received.load():0),(unsigned long long)cancelled_bytes,
        (unsigned long long)calls,ack,cleanup,cancelled,error.empty()?"none":error.c_str());
    if(socket>=0) ::close(socket);
    return !error.empty() || !ack || !cleanup || (!cancelled && (!started || (expected && completed!=expected) || !queue || completed!=queue->received));
}
