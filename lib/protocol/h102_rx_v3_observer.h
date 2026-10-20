/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file       h102_rx_v3_observer.h
 * @brief      Verify actual per-frame configuration/time continuity and retain bounded UTC-second boundary records.
 * @details    Single-thread callbacks avoid disk IO in USB service data paths; fail explicitly on capacity exhaustion.
 *             Require two ticks/IQ within one anchor; permit 1000 ppm source-clock error across PPS.
 * @author     LogicRF Technologies
 * @architect  LogicRF
 * @email      info@logicrf.com
 * @website    https://www.logicrf.com
 * @version    0.2.0
 * @date       2026-10-08
 * @note       Default entry is 40 MSPS; runtime entry checks package/board capability; no absolute RF-time accuracy claim.
 */
#ifndef H102_RX_V3_OBSERVER_H
#define H102_RX_V3_OBSERVER_H
#include <stdio.h>
#include "h102_usb_rx_stream_v3.h"
#define H102_V3_RECORDS 8192u /**< Dual-channel 1800 s plus state-change margin; stop on exhaustion. */
/** @brief Time state associated with the first sample; save complete configuration separately. */
struct h102_v3_record {
    unsigned channel;
    uint64_t first;
    uint32_t sequence, configuration_id;
    struct h102_rx_time_v3 time;
};
/** @brief Complete per-channel frame validation state with all counters and latest first sample. */
struct h102_v3_observed_channel {
    struct h102_rx_config_v3 expected;
    struct h102_rx_time_v3 previous;
    uint64_t first, valid_frames, invalid_frames, transitions, max_boundary_error_ticks;
    bool seen;
};
/** @brief Fixed-memory lifecycle: touch pages before RUN, write JSONL after STOP, main-thread ownership. */
struct h102_v3_observer {
    struct h102_rx_stream_v3 *parser;
    struct h102_v3_observed_channel channel[2];
    struct h102_v3_record records[H102_V3_RECORDS];
    unsigned count;
};
/** @brief Load expectations from the board's original 112-byte readback; caller persists its descriptor first. */
static bool h102_v3_expected(struct h102_v3_observer *o,const char *path,uint64_t stream)
{
    unsigned char b[112]; FILE *f=fopen(path,"rb"); if(!f) return false;
    bool ok=fread(b,1,sizeof(b),f)==sizeof(b) && fgetc(f)==EOF;
    if(fclose(f)) ok=false;
    if(!ok) return false;
    for(unsigned ch=0;ch<2;++ch) {
        const unsigned char *p=b+56*ch;
        struct h102_rx_config_v3 *e=&o->channel[ch].expected;
        *e=(struct h102_rx_config_v3){.rate_num=h102_le(p,8),.rate_den=h102_le(p+8,4),
            .bandwidth_hz=h102_le(p+12,4),.center_hz=h102_le(p+16,8),.id=h102_le(p+24,4),
            .manual_gain_mdb=(int32_t)h102_le(p+28,4),.flags=h102_le(p+32,4),
            .device_id=h102_le(p+36,8),.stream_id=h102_le(p+44,8),.gain_mode=p[52],.format=1};
        if(e->rate_num!=40000000 || e->rate_den!=1 || e->stream_id!=stream || !e->id ||
           p[53] || p[54] || p[55]) return false;
    }
    return true;
}
/**
 * @brief Explicit multirate entry: verify shared Fs/BW from complete configuration and set native 2Fs clock.
 * @param rate Integer Hz independently verified against approved package and all four channel attributes by the locked controller.
 * @param bandwidth Verified effective bandwidth in Hz; numerical range alone does not imply generated/qualified combinations.
 * @return true if configuration matches and parsing has not started; false if startup must be refused.
 * @note No device access here; caller checks exact bit/capability, package SHA, new configuration ID and time epoch.
 *       The fixed-rate entry never calls this; do not reset active time continuity with a new configuration.
 */
static inline bool h102_v3_expected_runtime(struct h102_v3_observer *o,const char *path,
    uint64_t stream,uint64_t rate,uint32_t bandwidth)
{
    if(!o || !o->parser || !stream || !bandwidth || bandwidth>rate || bandwidth>40000000 ||
       o->parser->stream_id!=stream || o->parser->error || o->parser->header_used ||
       o->parser->payload_left || o->parser->channel[0].frames || o->parser->channel[1].frames ||
       o->channel[0].seen || o->channel[1].seen || o->count) return false;
    switch(rate) {
    case 192000: case 768000: case 1536000: case 2000000: case 4000000:
    case 8000000: case 10000000: case 16000000: case 20000000: case 23040000:
    case 30720000: case 40000000: case 61440000: break;
    default: return false;
    }
    unsigned char b[112]; FILE *f=fopen(path,"rb"); if(!f) return false;
    bool ok=fread(b,1,sizeof(b),f)==sizeof(b) && fgetc(f)==EOF;
    if(fclose(f)) ok=false;
    if(!ok) return false;
    struct h102_rx_config_v3 expected[2];
    for(unsigned ch=0;ch<2;++ch) {
        const unsigned char *p=b+56*ch;
        expected[ch]=(struct h102_rx_config_v3){.rate_num=h102_le(p,8),.rate_den=h102_le(p+8,4),
            .bandwidth_hz=h102_le(p+12,4),.center_hz=h102_le(p+16,8),.id=h102_le(p+24,4),
            .manual_gain_mdb=(int32_t)h102_le(p+28,4),.flags=h102_le(p+32,4),
            .device_id=h102_le(p+36,8),.stream_id=h102_le(p+44,8),.gain_mode=p[52],.format=1};
        const struct h102_rx_config_v3 *e=&expected[ch];
        if(e->rate_num!=rate || e->rate_den!=1 || e->bandwidth_hz!=bandwidth ||
           e->stream_id!=stream || !e->id || !e->device_id ||
           e->center_hz<30000000 || e->center_hz>UINT64_C(6000000000) ||
           !((e->gain_mode==1 && e->flags==0x3f) ||
             (e->gain_mode==2 && e->flags==0x2f && e->manual_gain_mdb==0)) ||
           p[53] || p[54] || p[55]) return false;
    }
    if(expected[0].id!=expected[1].id || expected[0].device_id!=expected[1].device_id) return false;
    for(unsigned ch=0;ch<2;++ch) o->channel[ch].expected=expected[ch];
    o->parser->native_clock_hz=(uint32_t)(2*rate);
    return true;
}
/** @brief Check independent constraints after complete frames; parser latches first error and outer code requests STOP. */
static void h102_v3_observe(void *context,unsigned ch,uint64_t first,uint64_t samples,
                            const struct h102_rx_time_v3 *t,const struct h102_rx_config_v3 *c)
{
    struct h102_v3_observer *o=context;
    struct h102_v3_observed_channel *v=&o->channel[ch-1];
    const struct h102_rx_config_v3 *e=&v->expected;
    if(o->parser->channel[ch-1].missing) {o->parser->error="sample_gap";return;}
    if(c->rate_num!=e->rate_num || c->rate_den!=e->rate_den || c->center_hz!=e->center_hz ||
       c->bandwidth_hz!=e->bandwidth_hz || c->manual_gain_mdb!=e->manual_gain_mdb ||
       c->id!=e->id || c->flags!=e->flags || c->device_id!=e->device_id ||
       c->stream_id!=e->stream_id || c->gain_mode!=e->gain_mode || c->format!=1 || samples!=2048) {
        o->parser->error="configuration_readback_mismatch"; return;
    }
    if(t->valid) ++v->valid_frames; else ++v->invalid_frames;
    bool transition=!v->seen || t->valid!=v->previous.valid || t->epoch!=v->previous.epoch;
    if(transition) ++v->transitions;
    if(v->seen && t->valid && v->previous.valid && t->epoch==v->previous.epoch) {
        uint64_t delta=first-v->first;
        if(t->anchor_seconds<v->previous.anchor_seconds || delta>UINT32_MAX) {
            o->parser->error="timestamp_regression_or_unbounded_gap"; return;
        }
        uint64_t seconds=t->anchor_seconds-v->previous.anchor_seconds;
        if(seconds>60) {o->parser->error="timestamp_anchor_jump";return;}
        int64_t actual=(int64_t)(seconds*t->clock_hz)+(int64_t)t->offset_ticks-v->previous.offset_ticks;
        int64_t error=actual-(int64_t)(delta*2);
        uint64_t absolute=(uint64_t)(error<0?-error:error);
        if((!seconds && absolute) || (seconds && absolute>seconds*(t->clock_hz/1000)+4)) {
            o->parser->error="timestamp_sample_cadence";return;
        }
        if(seconds && absolute>v->max_boundary_error_ticks) v->max_boundary_error_ticks=absolute;
    }
    if(transition || (t->valid && t->anchor_seconds!=v->previous.anchor_seconds)) {
        if(o->count==H102_V3_RECORDS) {o->parser->error="timestamp_record_capacity";return;}
        o->records[o->count++]=(struct h102_v3_record){ch,first,c->frame_sequence,c->id,*t};
    }
    v->seen=true;v->first=first;v->previous=*t;
}
#endif
/* End of file: h102_rx_v3_observer.h */
