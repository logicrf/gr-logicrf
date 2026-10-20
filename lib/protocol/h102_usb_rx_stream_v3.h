/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file       h102_usb_rx_stream_v3.h
 * @brief      Bounded H102 v3 time-frame parsing; commit statistics/first-sample metadata for complete frames only.
 * @details    Fixed 128-byte header with byte fields/configuration snapshot; reject v1; native ticks are not IQ indices.
 * @author     LogicRF Technologies
 * @architect  LogicRF
 * @email      info@logicrf.com
 * @website    https://www.logicrf.com
 * @version    0.1.0
 * @date       2026-09-28
 * @note       Product-derived copy adds optional payload callbacks; original digital RX binaries stay unchanged; RF delay is uncalibrated.
 */
#ifndef H102_USB_RX_STREAM_V3_H
#define H102_USB_RX_STREAM_V3_H
#include "h102_usb_rx_stream.h"

/** @brief First-sample UTC = anchor_seconds + offset_ticks/clock_hz; all fields are zero when invalid. */
struct h102_rx_time_v3 {
    bool valid;                  /**< Associated UTC/PPS; RF delay may remain uncalibrated. */
    uint32_t epoch, clock_hz, offset_ticks;
    uint64_t anchor_seconds;     /**< UTC POSIX integer seconds; original GNSS fields remain in separate diagnostics. */
};
/** @brief Effective configuration at first sample; flags control validity and AGC never fabricates gain. */
struct h102_rx_config_v3 {
    uint64_t rate_num, center_hz, device_id, stream_id;
    uint32_t rate_den, bandwidth_hz, id, flags, frame_sequence, samples;
    int32_t manual_gain_mdb;
    uint8_t format, gain_mode;
};
/** @brief Complete-frame callback; time pointer lives only during callback; parser does not cache sample data. */
typedef void (*h102_rx_v3_frame_fn)(void *context, unsigned channel, uint64_t first,
                                   uint64_t samples, const struct h102_rx_time_v3 *time,
                                   const struct h102_rx_config_v3 *config);
/**
 * @brief Payload fragments after a valid header; false stops parsing without implying full-frame acceptance.
 * @details offset counts payload bytes and may be unaligned; data/metadata pointers live within the callback only.
 *          Copied fragments must await successful frame validation before IQ publication.
 */
typedef bool (*h102_rx_v3_payload_fn)(void *context, unsigned channel, uint64_t first,
    size_t offset, const unsigned char *data, size_t bytes,
    const struct h102_rx_time_v3 *time, const struct h102_rx_config_v3 *config);
/** @brief Single-thread stream state; zero-initialize and set mask, stream_id and native_clock_hz before use. */
struct h102_rx_stream_v3 {
    unsigned mask;
    uint32_t native_clock_hz;
    uint64_t stream_id;
    unsigned char header[128];
    size_t header_used, payload_left, payload_size;
    uint64_t offset, frame_offset;
    struct h102_rx_channel channel[2];
    struct h102_rx_time_v3 pending_time;
    struct h102_rx_config_v3 pending_config;
    const char *error;
    h102_gap_fn gap;
    h102_rx_v3_frame_fn frame;
    h102_rx_v3_payload_fn payload; /**< NULL retains original metadata-only parsing. */
    void *context;
};

/** @brief Validate v3 header, normalize invalid time and check ranges without early statistic commits. */
static inline bool h102_rx_v3_header(struct h102_rx_stream_v3 *s)
{
    const unsigned char *h=s->header;
    const uint64_t first=h102_le(h+24,8), bytes=h102_le(h+16,4);
    if (memcmp(h,"H102",4) || h[4]!=3 || h[5]!=1 || h[6]<1 || h[6]>2 || h[7]!=1 ||
        !bytes || bytes>65408 || bytes%4 || h102_le(h+8,2)!=128 || h102_le(h+60,4)!=bytes/4 ||
        h[10]>1 || h[11]>1 || h[12]>1 || h[13]>1 || h[14]!=h[13] || h[15]!=h[13] ||
        h[105]>1 || h[106]>1 || h[107]>1 || h[108]>1 || h[109]>1 ||
        h[110]!=h[13] || h[111]!=h[13]) {
        s->error="invalid_v3_header"; return false;
    }
    for (size_t i=112;i<128;++i) if (h[i]) {
        s->error="nonzero_reserved_extension"; return false;
    }
    if (!(s->mask&(1u<<(h[6]-1))) || !s->stream_id || h102_le(h+32,8)!=s->stream_id) {
        s->error="mask_or_stream_id"; return false;
    }
    const struct h102_rx_channel *c=&s->channel[h[6]-1];
    if (!!h[10]!=!c->frames || c->ended || first<c->next ||
        first>UINT64_MAX-bytes/4 || !!h[12]!=(first!=c->next) ||
        h102_le(h+20,4)!=(uint32_t)c->frames || c->frames==UINT64_MAX || c->samples>UINT64_MAX-bytes/4 ||
        c->missing>UINT64_MAX-(first-c->next)) {
        s->error="sequence_or_flags"; return false;
    }
    struct h102_rx_time_v3 time={
        .valid=!!h[13], .epoch=(uint32_t)h102_le(h+44,4), .anchor_seconds=h102_le(h+80,8),
        .offset_ticks=(uint32_t)h102_le(h+88,4), .clock_hz=(uint32_t)h102_le(h+92,4)
    };
    if (!time.valid) {
        if(time.epoch || time.anchor_seconds || time.offset_ticks || time.clock_hz) {
            s->error="noncanonical_invalid_time"; return false;
        }
    } else if (!time.epoch || !s->native_clock_hz || time.clock_hz!=s->native_clock_hz ||
               time.anchor_seconds<UINT64_C(315964800) || time.anchor_seconds>UINT64_C(39952137599) ||
               time.offset_ticks>(uint64_t)time.clock_hz+time.clock_hz/1000) {
        s->error="invalid_sample_time"; return false;
    }
    struct h102_rx_config_v3 config={
        .format=h[7], .gain_mode=h[104], .rate_num=h102_le(h+48,8),
        .rate_den=(uint32_t)h102_le(h+56,4), .bandwidth_hz=(uint32_t)h102_le(h+72,4),
        .center_hz=h102_le(h+64,8), .id=(uint32_t)h102_le(h+40,4),
        .frame_sequence=(uint32_t)h102_le(h+20,4), .manual_gain_mdb=(int32_t)h102_le(h+76,4),
        // Internal flags preserve API compatibility; wire status fields are individual uint8 values; mode=0 means unknown.
        .flags=h[105] | (h[106]<<1) | (h[107]<<2) | ((h[104]!=0)<<3) | (h[108]<<4) | (h[109]<<5),
        .samples=(uint32_t)h102_le(h+60,4), .device_id=h102_le(h+96,8), .stream_id=h102_le(h+32,8)
    };
    if (!(config.flags&1) || !config.rate_num || !config.rate_den || !config.id || config.gain_mode>2 ||
        (!(config.flags&2) && config.center_hz) || ((config.flags&2) && !config.center_hz) ||
        (!(config.flags&4) && config.bandwidth_hz) || ((config.flags&4) && !config.bandwidth_hz) ||
        (!(config.flags&16) && config.manual_gain_mdb) || ((config.flags&16) && config.gain_mode!=1) ||
        (!(config.flags&32) && config.device_id) || ((config.flags&32) && !config.device_id)) {
        s->error="invalid_applied_configuration"; return false;
    }
    s->pending_time=time;
    s->pending_config=config;
    s->payload_size=s->payload_left=(size_t)bytes;
    return true;
}

/** @brief Publish time/gap callbacks for complete IQ frames only; truncated tails add no samples. */
static inline void h102_rx_v3_commit(struct h102_rx_stream_v3 *s)
{
    const unsigned ch=s->header[6];
    struct h102_rx_channel *c=&s->channel[ch-1];
    const uint64_t first=h102_le(s->header+24,8), samples=s->payload_size/4;
    if (first!=c->next && s->gap) s->gap(s->context,ch,s->frame_offset,c->next,first);
    c->missing+=first-c->next;
    c->next=first+samples;
    c->frames++;
    c->samples+=samples;
    c->ended=!!s->header[11];
    if (s->frame) s->frame(s->context,ch,first,samples,&s->pending_time,&s->pending_config);
    s->header_used=0;
    s->frame_offset=s->offset;
}

/** @brief Consume arbitrary chunks; permanently fail on first error without searching past corruption. */
static inline bool h102_rx_v3_feed(struct h102_rx_stream_v3 *s, const unsigned char *data, size_t bytes)
{
    if (s->error) return false;
    if (bytes>UINT64_MAX-s->offset) { s->error="stream_offset_overflow"; return false; }
    while (bytes) {
        if (s->header_used<128) {
            size_t take=128-s->header_used;
            if (take>bytes) take=bytes;
            memcpy(s->header+s->header_used,data,take);
            s->header_used+=take; s->offset+=take; data+=take; bytes-=take;
            if (s->header_used==128 && !h102_rx_v3_header(s)) return false;
        } else {
            size_t take=s->payload_left<bytes?s->payload_left:bytes;
            if (s->payload && !s->payload(s->context,s->header[6],h102_le(s->header+24,8),
                    s->payload_size-s->payload_left,data,take,&s->pending_time,&s->pending_config)) {
                if (!s->error) s->error="payload_rejected";
                return false;
            }
            s->payload_left-=take; s->offset+=take; data+=take; bytes-=take;
            if (!s->payload_left) {
                h102_rx_v3_commit(s);
                if (s->error) return false; // Complete-frame callbacks can also reject application configuration/continuity.
            }
        }
    }
    return true;
}
#endif
/* End of file: h102_usb_rx_stream_v3.h */
