/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_metadata_sink.cc
 * @brief Batch H1MI index writes while preserving native integer metadata and strict file-error propagation.
 * @details Construction has no IO; start creates exclusively; stop fsyncs; never overwrite or synthesize IIO time.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.7.0
 * @date 2026-10-16
 ******************************************************************************/
#include <gnuradio/logicrf/h102_metadata_sink.h>
#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>
#include <pmt/pmt.h>
#include <atomic>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <mutex>
#include <stdexcept>
#include <unistd.h>
#include <vector>
namespace gr::logicrf {
namespace {
void put(unsigned char* data,std::uint64_t value,unsigned count)
{for(unsigned i=0;i<count;++i)data[i]=static_cast<unsigned char>(value>>(8*i));}
std::uint64_t field(const pmt::pmt_t& value,const char* name)
{
    auto result=pmt::dict_ref(value,pmt::intern(name),pmt::PMT_NIL);
    if(!pmt::is_uint64(result))throw std::runtime_error(std::string("METADATA_FIELD:")+name);
    return pmt::to_uint64(result);
}
}
/** @brief One worker owns fd; external status reads use atomic counters and mutex-protected errors. */
class metadata_sink_impl final : public h102_metadata_sink {
public:
    metadata_sink_impl(device::sptr board,std::string path,unsigned channel)
        : gr::sync_block("h102_metadata_sink",gr::io_signature::make(1,1,sizeof(gr_complex)),
            gr::io_signature::make(0,0,0)),board(std::move(board)),path(std::move(path)),channel(channel)
    {
        if(!this->board || this->board->options().direction!="rx" || channel<1 || channel>2 || this->path.empty())
            throw std::invalid_argument("METADATA_RX_DEVICE_PATH_CHANNEL");
    }
    ~metadata_sink_impl() override {close_file();}
    bool start() override
    {
        running=true;
        count=0;{std::lock_guard<std::mutex> lock(mutex);failure.clear();}
        fd=::open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
        if(fd<0)fail("OPEN",false);
        return true;
    }
    bool stop() override {close_file();running=false;return true;}
    void set_path(const std::string& value) override
    {
        if(running || value.empty())throw std::runtime_error("METADATA_SET_PATH_REQUIRES_STOPPED");
        path=value;
    }
    std::uint64_t records() const override {return count.load();}
    std::string error() const override {std::lock_guard<std::mutex> lock(mutex);return failure;}
    int work(int size,gr_vector_const_void_star&,gr_vector_void_star&) override
    {
        if(!error().empty()){board->invalidate(error());return WORK_DONE;}
        try {
            std::vector<gr::tag_t> tags;
            get_tags_in_range(tags,0,nitems_read(0),nitems_read(0)+size,pmt::intern("h102_rx_frame"));
            std::vector<unsigned char> output(tags.size()*128,0);
            for(std::size_t i=0;i<tags.size();++i) {
                const auto& tag=tags[i];const auto& value=tag.value;auto* row=output.data()+128*i;
                if(field(value,"channel")!=channel || field(value,"frame_samples")!=2048 || !field(value,"time_valid"))
                    throw std::runtime_error("METADATA_FRAME_CHANNEL_GEOMETRY_TIME");
                std::memcpy(row,"H1MI",4);row[4]=1;row[5]=channel;row[6]=field(value,"time_valid");
                row[7]=field(value,"gain_mode");
                put(row+8,tag.offset,8);put(row+16,field(value,"first_sample_index"),8);
                put(row+24,field(value,"utc_anchor_seconds"),8);put(row+32,field(value,"offset_ticks"),4);
                put(row+36,field(value,"tick_hz"),4);put(row+40,field(value,"frame_sequence"),4);
                put(row+44,field(value,"time_epoch"),4);put(row+48,field(value,"frame_samples"),4);
                put(row+52,field(value,"configuration_id"),4);put(row+56,field(value,"stream_id"),8);
                put(row+64,field(value,"device_id"),8);put(row+72,field(value,"rate_num"),8);
                put(row+80,field(value,"rate_den"),4);put(row+84,field(value,"bandwidth_hz"),4);
                put(row+88,field(value,"center_frequency_hz"),8);
                auto gain=pmt::dict_ref(value,pmt::intern("gain_mdb"),pmt::PMT_NIL);
                if(!pmt::is_integer(gain))throw std::runtime_error("METADATA_GAIN_FIELD");
                put(row+96,static_cast<std::uint32_t>(pmt::to_long(gain)),4);
            }
            std::size_t offset=0;
            while(offset<output.size()) {
                auto written=::write(fd,output.data()+offset,output.size()-offset);
                if(written<0 && errno==EINTR)continue;
                if(written<=0){fail("WRITE");return WORK_DONE;}
                offset+=static_cast<std::size_t>(written);
            }
            count+=tags.size();return size;
        } catch(const std::exception& exception) {
            {std::lock_guard<std::mutex> lock(mutex);if(failure.empty())failure=exception.what();}
            board->invalidate(error());return WORK_DONE;
        }
    }
private:
    void fail(const char* operation,bool invalidate=true)
    {
        const auto reason=std::string("METADATA_")+operation+":"+std::strerror(errno);
        {std::lock_guard<std::mutex> lock(mutex);if(failure.empty())failure=reason;}
        if(invalidate)board->invalidate(error());
    }
    void close_file() noexcept
    {
        if(fd<0)return;
        if(::fsync(fd))fail("FSYNC");
        if(::close(fd))fail("CLOSE");
        fd=-1;
    }
    device::sptr board;
    std::string path;
    unsigned channel;
    int fd=-1;
    std::atomic<std::uint64_t> count{0};
    std::atomic<bool> running{false}; ///< External lifecycle calls serialize path configuration.
    mutable std::mutex mutex;
    std::string failure;
};
h102_metadata_sink::sptr h102_metadata_sink::make(device::sptr board,const std::string& path,unsigned channel)
{return gnuradio::make_block_sptr<metadata_sink_impl>(std::move(board),path,channel);}
} // namespace gr::logicrf
