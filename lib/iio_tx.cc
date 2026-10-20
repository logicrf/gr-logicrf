/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file iio_tx.cc
 * @brief Noncyclic IQ16 network submission with strict scan validation and exception cleanup.
 * @details Decouple small DMA blocks from requests; batch at most eight existing blocks and account only after final reply.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.7.0
 * @date 2026-10-19
 * @note Submitted bytes are not DMA/SSI consumption; independent ownership still manages RF.
 ******************************************************************************/
#include "iio_tx.h"
#include "iio_tx_wire.h"
#include <iio.h>
#include <cerrno>
#include <algorithm>
#include <cmath>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace gr::logicrf {
struct iio_tx::impl {
    std::string uri;
    unsigned channel;
    std::size_t samples;
    double sample_rate;
    hooks callback;
    std::mutex mutex;
    std::shared_ptr<iio_tx_wire> wire;
};
iio_tx::iio_tx(std::string uri,unsigned channel,std::size_t samples,double sample_rate,hooks callback)
    : d(std::make_unique<impl>())
{
    d->uri=std::move(uri);d->channel=channel;d->samples=samples;d->sample_rate=sample_rate;
    d->callback=std::move(callback);
}
iio_tx::~iio_tx()=default;
void iio_tx::cancel() noexcept
{
    std::shared_ptr<iio_tx_wire> wire;
    {std::lock_guard<std::mutex> lock(d->mutex);wire=d->wire;}
    if(wire) wire->cancel();
}
void iio_tx::run() noexcept
{
    iio_context* context=nullptr;iio_device* device=nullptr;bool changed=false;
    try {
        auto check=[this] {
            if(d->callback.stopping()) throw std::runtime_error("IIO_TX_CANCELLED");
            if(!d->callback.permitted()) throw std::runtime_error("IIO_TX_PERMISSION_EXPIRED");
        };
        check();
        if(!std::isfinite(d->sample_rate) || d->sample_rate<=0 || d->sample_rate>16000000 || !d->samples)
            throw std::runtime_error("IIO_TX_SAMPLE_RATE");
        const auto pacing=static_cast<std::uint64_t>(std::ceil(d->sample_rate*4*5/4));
        // Long batches await device playback; low rates need more than a fixed eight-block cap to bound replies.
        const auto batch_limit=static_cast<unsigned>(std::clamp(
            std::floor(d->sample_rate*.1/d->samples),1.0,8.0));
        unsigned major=0,minor=0;char git[8]{};
        iio_library_get_version(&major,&minor,git);
        if(major!=0 || minor<23) throw std::runtime_error("LIBIIO_ABI_NOT_0_23_PLUS");
        context=iio_create_context_from_uri(d->uri.c_str());
        if(!context) throw std::runtime_error("IIO_TX_CONTEXT:"+std::to_string(errno));
        if(iio_context_set_timeout(context,1000)<0) throw std::runtime_error("IIO_TX_TIMEOUT_SET");
        device=iio_context_find_device(context,d->channel==1?"axi-adrv9002-tx-lpc":"axi-adrv9002-tx2-lpc");
        if(!device) throw std::runtime_error("IIO_TX_DEVICE");
        for(unsigned n=0;n<iio_device_get_channels_count(device);++n)
            iio_channel_disable(iio_device_get_channel(device,n));
        iio_channel* scan[2]={iio_device_find_channel(device,"voltage0",true),iio_device_find_channel(device,"voltage1",true)};
        for(int n=0;n<2;++n) {
            if(!scan[n] || iio_channel_get_index(scan[n])!=n) throw std::runtime_error("IIO_TX_SCAN_INDEX");
            const auto* f=iio_channel_get_data_format(scan[n]);
            if(f->length!=16 || f->bits!=16 || f->shift || !f->is_signed || f->is_be || f->repeat!=1)
                throw std::runtime_error("IIO_TX_SCAN_FORMAT");
            iio_channel_enable(scan[n]);
        }
        if(iio_device_get_sample_size(device)!=4) throw std::runtime_error("IIO_TX_SCAN_SIZE");
        const unsigned kernel_buffers=d->sample_rate>=8000000 ? 16 : 4;
        if(iio_device_set_kernel_buffers_count(device,kernel_buffers)<0)
            throw std::runtime_error("IIO_TX_KERNEL_BUFFERS");
        changed=true;check();
        auto wire=std::make_shared<iio_tx_wire>(check);
        {std::lock_guard<std::mutex> lock(d->mutex);d->wire=wire;}
        // Validate every scan field; open verified IQ16 only, without private libiio structures.
        std::string mask(((iio_device_get_channels_count(device)+31)/32)*8,'0');
        mask.back()='3';
        wire->open(d->uri,iio_device_get_id(device),d->samples,mask,pacing);
        check();d->callback.guard("iio-tx-begin");d->callback.ready();
        std::vector<std::uint32_t> words;
        while(d->callback.take(words)) {
            check();
            if(words.empty() || words.size()>d->samples) throw std::runtime_error("IIO_TX_BLOCK_SIZE");
            // Combine existing blocks only; never wait for future input, pad short tails or replay at stop.
            if(d->samples>=65536 && d->callback.take_available) {
                std::vector<std::uint32_t> next;
                for(unsigned count=1;count<batch_limit && d->callback.take_available(next);++count) {
                    if(next.empty() || next.size()>d->samples) throw std::runtime_error("IIO_TX_BLOCK_SIZE");
                    if(count==1) words.reserve(8*d->samples);
                    words.insert(words.end(),next.begin(),next.end());next.clear();
                }
            }
            d->callback.attempting(words.size());
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
            // Borrow packed bytes only on compile-time LE; retain words until the exact final reply.
            wire->push(words.data(),words.size()*sizeof(std::uint32_t));
#else
            // Other byte orders explicitly produce LE; never send unused buffer tails.
            std::vector<unsigned char> payload(words.size()*4);
            for(std::size_t n=0;n<words.size();++n) for(unsigned b=0;b<4;++b)
                payload[4*n+b]=static_cast<unsigned char>(words[n]>>(8*b));
            wire->push(payload.data(),payload.size());
#endif
            d->callback.submitted(words);check();
        }
        check();d->callback.guard("iio-tx-end");
        wire->close();
    } catch(const std::exception& error) {
        if(!d->callback.stopping()) d->callback.fail(error.what());
    }
    {
        std::lock_guard<std::mutex> lock(d->mutex);
        d->wire.reset();
    }
    if(context) {
        if(changed && iio_device_set_kernel_buffers_count(device,4)<0) d->callback.fail("IIO_TX_KERNEL_RESTORE");
        iio_context_destroy(context);
    }
}
} // namespace gr::logicrf
