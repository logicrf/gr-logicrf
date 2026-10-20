/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file ft601_duplex.cc
 * @brief One FT601 handle serves 1RX1TX using two C++ local sockets.
 * @details Separate bounded queues/128 slots per direction; keep RX until board STOP/ACK after END. DRAIN/A/FLUSHED/Q,
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
constexpr unsigned rx_block=65536,rx_ring_slots=1024;
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
/** @brief Fixed 64 MiB RX queue; fail slow consumers explicitly without overwriting old IQ. */
class wire_queue {
public:
    explicit wire_queue(int socket):socket(socket),storage(std::size_t(rx_ring_slots)*rx_block),lengths(rx_ring_slots) {
        for(std::size_t p=0;p<storage.size();p+=4096) storage[p]=0;
        writer=std::thread([this]{run();});
    }
    ~wire_queue() {finish();}
    void push(const unsigned char* data,std::size_t bytes) {
        std::lock_guard<std::mutex> lock(mutex);
        if(failed || used==rx_ring_slots) throw std::runtime_error("USB_WIRE_QUEUE_OVERFLOW");
        std::memcpy(storage.data()+std::size_t(tail)*rx_block,data,bytes);lengths[tail]=bytes;
        tail=(tail+1)%rx_ring_slots;++used;peak=std::max(peak,used);changed.notify_one();
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
            const auto* data=storage.data()+std::size_t(slot)*rx_block;
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
            {std::lock_guard<std::mutex> lock(mutex);head=(head+1)%rx_ring_slots;--used;}
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
    if(argc!=10 || ::getuid()==0) {
        std::fprintf(stderr,"usage: ft601_duplex SERIAL TX_SOCKET RX_SOCKET SECONDS RATE RX_MASK TX_MASK SAMPLES PARENT\n");return 2;
    }
    std::setvbuf(stdout,nullptr,_IOLBF,0);std::signal(SIGPIPE,SIG_IGN);
    FT_HANDLE handle=nullptr;std::array<slot,depth> tx_slots,rx_slots;
    std::unique_ptr<input_queue> input;std::unique_ptr<wire_queue> output;
    int tx_fd=-1,rx_fd=-1;bool tx_stream=false,rx_stream=false,ack=false,ready=false,started=false;
    bool cancelled=false,stop_requested=false,cleanup=true;double stop_at=0;
    std::string error;std::uint64_t submitted=0,completed=0,expected=0,rx_bytes=0,cancelled_tx=0,tail_rx=0;
    unsigned rx_index=0,unknown=0;std::uint64_t calls=0;
    try {
        auto seconds=number(argv[4]),rate=number(argv[5]),rx_mask=number(argv[6]),tx_mask=number(argv[7]);
        auto samples=number(argv[8]),parent=number(argv[9]);
        if(std::strcmp(argv[1],"000000000001") || seconds>300 || rate<192000 || rate>40000000 ||
           (rx_mask!=1 && rx_mask!=2) || (tx_mask!=1 && tx_mask!=2) ||
           ((seconds==0)!=(samples==0)) || (samples && samples<16384) || samples%16384 ||
           (samples && samples>rate*(seconds+1)) || parent<=1 || parent>2147483647)
            throw std::runtime_error("DUPLEX_ARGUMENT_RANGE");
        if(prctl(PR_SET_PDEATHSIG,SIGKILL)!=0 || getppid()!=static_cast<pid_t>(parent))
            throw std::runtime_error("DUPLEX_CONTROLLER_PARENT");
        auto connect_socket=[](const char* path) {
            sockaddr_un address{};address.sun_family=AF_UNIX;
            if(std::strlen(path)>=sizeof(address.sun_path))throw std::runtime_error("DUPLEX_SOCKET_PATH");
            std::strcpy(address.sun_path,path);int fd=::socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
            if(fd<0)throw std::runtime_error("DUPLEX_SOCKET_CREATE");
            auto deadline=now()+15;
            while(::connect(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address))) {
                if(now()>deadline){::close(fd);throw std::runtime_error("DUPLEX_SOCKET_CONNECT");}
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            ucred peer{};socklen_t length=sizeof(peer);
            if(::getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&length) || peer.uid!=::getuid()) {
                ::close(fd);throw std::runtime_error("DUPLEX_SOCKET_PEER_UID");
            }
            return fd;
        };
        tx_fd=connect_socket(argv[2]);rx_fd=connect_socket(argv[3]);
        expected=samples/2048*8320;
        if(h102_dma_initialize())throw std::runtime_error("DUPLEX_SDK_LIBUSB_PIN");
        FT_TRANSFER_CONF conf{};conf.wStructSize=sizeof(conf);
        for(unsigned i=0;i<2;++i){conf.pipe[i].bURBCount=depth;conf.pipe[i].dwURBBufferSize=32768;}
        if(FT_SetTransferParams(&conf,0)!=FT_OK || FT_Create(argv[1],FT_OPEN_BY_SERIAL_NUMBER,&handle)!=FT_OK)
            throw std::runtime_error("DUPLEX_USB_OPEN");
        FT_60XCONFIGURATION chip{};
        if(FT_GetChipConfiguration(handle,&chip)!=FT_OK || chip.VendorID!=0x0403 || chip.ProductID!=0x601f ||
           chip.FIFOClock!=0 || chip.FIFOMode!=0 || chip.ChannelConfig!=2)throw std::runtime_error("DUPLEX_USB_CHIP");
        std::printf("DRAIN_REQUEST\n");control('A',8);
        if(FT_AbortPipe(handle,0x02)!=FT_OK || FT_AbortPipe(handle,0x82)!=FT_OK)
            throw std::runtime_error("DUPLEX_INITIAL_ABORT");
        unsigned char drain[1024];bool empty=false;
        for(unsigned n=0;n<256;++n){ULONG got=0;auto status=FT_ReadPipeEx(handle,0,drain,sizeof(drain),&got,100);
            if(got>sizeof(drain) || (status!=FT_OK && status!=FT_TIMEOUT))break;
            if(!got){empty=true;break;}}
        if(!empty)throw std::runtime_error("DUPLEX_RX_INITIAL_DRAIN");
        std::printf("FLUSHED\n");control('Q',5);
        const DWORD timeout=static_cast<DWORD>(15000+std::uint64_t(depth)*rx_block*1000/(4*rate));
        if(FT_SetPipeTimeout(handle,0x82,timeout)!=FT_OK || FT_SetPipeTimeout(handle,0x02,timeout)!=FT_OK)
            throw std::runtime_error("DUPLEX_USB_TIMEOUT");
        std::deque<unsigned> free,pending;
        for(unsigned i=0;i<depth;++i){
            tx_slots[i].data.resize(block);rx_slots[i].data.resize(rx_block);
            for(auto* s:{&tx_slots[i],&rx_slots[i]}) {
                if(FT_InitializeOverlapped(handle,&s->op)!=FT_OK)throw std::runtime_error("DUPLEX_USB_EVENT");
                s->initialized=true;
            }
            free.push_back(i);
        }
        if(FT_SetStreamPipe(handle,FALSE,FALSE,0x82,rx_block)!=FT_OK)throw std::runtime_error("DUPLEX_RX_STREAM");
        rx_stream=true;
        if(FT_SetStreamPipe(handle,FALSE,FALSE,0x02,block)!=FT_OK)throw std::runtime_error("DUPLEX_TX_STREAM");
        tx_stream=true;
        output=std::make_unique<wire_queue>(rx_fd);input=std::make_unique<input_queue>(tx_fd,expected);
        auto submit_rx=[&](slot& s){
            ULONG got=0;s.op.Internal=0;s.op.InternalHigh=0;
            auto status=FT_ReadPipeAsync(handle,0,s.data.data(),rx_block,&got,&s.op);
            if(status!=FT_OK && status!=FT_IO_PENDING)throw std::runtime_error("DUPLEX_RX_SUBMIT");
            s.pending=true;
        };
        for(auto& s:rx_slots)submit_rx(s);
        std::printf("RX_QUEUED\n");double beginning=now(),tx_progress=beginning,rx_progress=beginning;
        for(;;){
            pollfd p{STDIN_FILENO,POLLIN,0};auto event=::poll(&p,1,0);
            if(event<0 && errno!=EINTR)throw std::runtime_error("DUPLEX_CONTROL_POLL");
            if(event>0){
                char c=0;if(!(p.revents&POLLIN) || ::read(STDIN_FILENO,&c,1)!=1)throw std::runtime_error("DUPLEX_CONTROLLER_LOST");
                if(c=='\n' && stop_requested){ack=true;break;}
                if(c=='S' && ready && !started)started=true;
                else if(c=='C' && !stop_requested){cancelled=true;stop_requested=true;stop_at=now();std::printf("CANCELLED\nSTOP_REQUEST\n");}
                else throw std::runtime_error("DUPLEX_CONTROL_PHASE");
            }
            if(input->failed || output->failed)throw std::runtime_error("DUPLEX_SOCKET_QUEUE_FAILED");
            if(!stop_requested){
                for(unsigned n=0;n<32 && !free.empty() && (!expected || submitted<expected);++n){
                    auto index=free.front();auto& s=tx_slots[index];if(!input->pop(s.data.data()))break;
                    ULONG got=0;s.op.Internal=0;s.op.InternalHigh=0;
                    auto status=FT_WritePipeAsync(handle,0,s.data.data(),block,&got,&s.op);
                    if(status!=FT_OK && status!=FT_IO_PENDING)throw std::runtime_error("DUPLEX_TX_SUBMIT");
                    s.pending=true;pending.push_back(index);free.pop_front();submitted+=block;
                }
                if(!ready && submitted>=2*block){ready=true;std::printf("READY\n");}
                for(unsigned n=0;n<32 && !pending.empty();++n){
                    auto index=pending.front();auto& s=tx_slots[index];ULONG got=0;
                    auto status=completed_read(handle,&s.op,&got);++calls;
                    if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING)break;
                    if(status!=FT_OTHER_ERROR)s.pending=false;
                    if(status!=FT_OK || got!=block)throw std::runtime_error("DUPLEX_TX_COMPLETION");
                    completed+=got;tx_progress=now();pending.pop_front();free.push_back(index);
                }
                if(started && input->finished && completed==input->received && (!expected || completed==expected)){
                    stop_requested=true;stop_at=now();std::printf("USB_DONE\nSTOP_REQUEST\n");
                }
            }
            // Keep IN reads after TX END/cancel until verified board STOP/DRAIN authorizes ACK.
            for(unsigned n=0;n<32;++n){
                auto& s=rx_slots[rx_index];ULONG got=0;auto status=completed_read(handle,&s.op,&got);++calls;
                if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING)break;
                if(status!=FT_OTHER_ERROR)s.pending=false;
                if(status!=FT_OK || got>rx_block)throw std::runtime_error("DUPLEX_RX_COMPLETION");
                if(got){output->push(s.data.data(),got);rx_bytes+=got;rx_progress=now();}
                submit_rx(s);rx_index=(rx_index+1)%depth;
            }
            if((stop_requested && now()-stop_at>8) || (!stop_requested &&
               ((seconds && now()-beginning>seconds+25) || now()-tx_progress>15 || now()-rx_progress>15)))
                throw std::runtime_error("DUPLEX_PROGRESS_OR_ACK_DEADLINE");
            std::this_thread::sleep_for(std::chrono::microseconds(25));
        }
    } catch(const std::exception& exception){error=exception.what();std::printf("ERROR %s\n",error.c_str());}
    if(handle && !ack){
        std::printf("USB_FAILED\nSTOP_REQUEST\n");
        try{control('\n',8);ack=true;}catch(const std::exception& exception){if(error.empty())error=exception.what();}
    }
    if(handle){
        if(!ack){std::printf("RESULT ack=0 cleanup=0\n");std::fflush(stdout);::_exit(75);}
        if(input)input->cancel();
        if(h102_dma_quiesce(2000)){std::printf("UNQUIESCED\n");std::fflush(stdout);::_exit(74);}
        bool prefix=true;
        for(unsigned n=0;n<depth;++n){
            auto& s=rx_slots[(rx_index+n)%depth];if(!s.pending)continue;
            ULONG got=0;auto status=completed_read(handle,&s.op,&got);
            if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING || status==FT_OTHER_ERROR){++unknown;continue;}
            s.pending=false;
            if(got>rx_block){++unknown;continue;}
            if(status==FT_OK && prefix){
                try{if(got){output->push(s.data.data(),got);rx_bytes+=got;}}
                catch(const std::exception& exception){error=exception.what();prefix=false;tail_rx+=got;}
            }else{prefix=false;tail_rx+=got;}
        }
        for(auto& s:tx_slots)if(s.pending){
            ULONG got=0;auto status=completed_read(handle,&s.op,&got);
            if(status==FT_IO_INCOMPLETE || status==FT_IO_PENDING || status==FT_OTHER_ERROR)++unknown;
            else{s.pending=false;cancelled_tx+=got;}
        }
        if(unknown){std::printf("UNKNOWN_CALLBACKS %u\n",unknown);std::fflush(stdout);::_exit(74);}
        if(tx_stream){cleanup=FT_AbortPipe(handle,0x02)==FT_OK && cleanup;cleanup=FT_ClearStreamPipe(handle,FALSE,FALSE,0x02)==FT_OK && cleanup;}
        if(rx_stream){cleanup=FT_AbortPipe(handle,0x82)==FT_OK && cleanup;cleanup=FT_ClearStreamPipe(handle,FALSE,FALSE,0x82)==FT_OK && cleanup;}
        for(unsigned i=0;i<depth;++i)for(auto* s:{&tx_slots[i],&rx_slots[i]})
            if(s->initialized)cleanup=FT_ReleaseOverlapped(handle,&s->op)==FT_OK && cleanup;
        cleanup=FT_Close(handle)==FT_OK && cleanup;
    }
    if(output)output->finish();
    std::printf("RESULT submitted=%llu completed=%llu expected=%llu received=%llu rx_bytes=%llu rx_sent=%llu rx_tail_bytes=%llu cancelled_tx=%llu calls=%llu ack=%u cleanup=%u cancelled=%u error=%s\n",
        (unsigned long long)submitted,(unsigned long long)completed,(unsigned long long)expected,
        (unsigned long long)(input?input->received.load():0),(unsigned long long)rx_bytes,
        (unsigned long long)(output?output->sent:0),(unsigned long long)tail_rx,(unsigned long long)cancelled_tx,
        (unsigned long long)calls,ack,cleanup,cancelled,error.empty()?"none":error.c_str());
    if(tx_fd>=0)::close(tx_fd);
    if(rx_fd>=0)::close(rx_fd);
    return !error.empty() || !ack || !cleanup || (output && output->failed) ||
        (!cancelled && (!started || (expected && completed!=expected) || !input || completed!=input->received));
}
