/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_frame.c
 * @brief Integrate pinned Linux v3 parser/configuration/time observer with complete IQ delivery.
 * @details Stage arbitrary payload fragments and call consumers after frame validation only; errors never publish staged samples.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.3.0
 * @date 2026-10-16
 * @note Linux source provenance is recorded in docs/usb-protocol.lock.json.
 ******************************************************************************/
#include "usb_frame.h"
#include "protocol/h102_rx_v3_observer.h"
#include <stdlib.h>

struct usb_decoder {
    struct h102_rx_stream_v3 parser;
    struct h102_v3_observer observer;
    unsigned char tentative[65408];
    size_t used;
    uint64_t accepted_frames[2], accepted_samples[2];
    uint32_t epoch;
    usb_frame_fn callback;
    void* context;
};
static bool payload(void* context,unsigned channel,uint64_t first,size_t offset,
    const unsigned char* data,size_t bytes,const struct h102_rx_time_v3* time,
    const struct h102_rx_config_v3* config)
{
    (void)channel;(void)first;(void)time;(void)config;
    struct usb_decoder* d=context;
    if(offset!=d->used || offset>sizeof(d->tentative) || bytes>sizeof(d->tentative)-offset) return false;
    memcpy(d->tentative+offset,data,bytes);d->used+=bytes;return true;
}
static void complete(void* context,unsigned channel,uint64_t first,uint64_t samples,
    const struct h102_rx_time_v3* t,const struct h102_rx_config_v3* c)
{
    struct usb_decoder* d=context;
    // Time checks depend on each channel's previous frame, not the historical record array; deliver originals via tags.
    // Retain current-frame records only so hours of continuous reception cannot exhaust diagnostic storage.
    d->observer.count=0;
    h102_v3_observe(&d->observer,channel,first,samples,t,c);
    if(d->parser.error) return;
    if(!t->valid || t->epoch!=d->epoch) {d->parser.error="usb_time_invalid_or_epoch";return;}
    if(d->used!=samples*4) {d->parser.error="usb_payload_size";return;}
    struct usb_frame_metadata m={.first=first,.anchor_seconds=t->anchor_seconds,.stream_id=c->stream_id,
        .device_id=c->device_id,.rate_num=c->rate_num,.center_hz=c->center_hz,.samples=(uint32_t)samples,
        .sequence=c->frame_sequence,.config_id=c->id,.time_epoch=t->epoch,.clock_hz=t->clock_hz,
        .offset_ticks=t->offset_ticks,.rate_den=c->rate_den,.bandwidth_hz=c->bandwidth_hz,
        .gain_mdb=c->manual_gain_mdb,.channel=(uint8_t)channel,.time_valid=t->valid,.gain_mode=c->gain_mode};
    if(d->callback(d->context,d->tentative,d->used,&m)) {d->parser.error="usb_consumer_rejected";return;}
    ++d->accepted_frames[channel-1];d->accepted_samples[channel-1]+=samples;
    d->used=0;
}
struct usb_decoder* usb_decoder_new(unsigned mask,uint64_t stream,uint32_t epoch,
    uint64_t rate,uint32_t bandwidth,const char* configuration,usb_frame_fn callback,void* context)
{
    if(mask<1 || mask>3 || !epoch || !callback) return NULL;
    struct usb_decoder* d=calloc(1,sizeof(*d));if(!d) return NULL;
    d->parser.mask=mask;d->parser.stream_id=stream;d->parser.context=d;
    d->parser.frame=complete;d->parser.payload=payload;d->observer.parser=&d->parser;
    d->epoch=epoch;d->callback=callback;d->context=context;
    if(!h102_v3_expected_runtime(&d->observer,configuration,stream,rate,bandwidth)) {free(d);return NULL;}
    return d;
}
const char* usb_decoder_feed(struct usb_decoder* d,const unsigned char* data,size_t bytes)
{
    if(!d) return "usb_decoder_not_initialized";
    if(!h102_rx_v3_feed(&d->parser,data,bytes)) return d->parser.error;
    return NULL;
}
size_t usb_decoder_partial(const struct usb_decoder* d) {return d->parser.header_used+d->used;}
uint64_t usb_decoder_samples(const struct usb_decoder* d,unsigned channel) {return channel>=1&&channel<=2?d->accepted_samples[channel-1]:0;}
uint64_t usb_decoder_frames(const struct usb_decoder* d,unsigned channel) {return channel>=1&&channel<=2?d->accepted_frames[channel-1]:0;}
void usb_decoder_free(struct usb_decoder* d) {free(d);}
