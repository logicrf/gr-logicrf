/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file       h102_usb_rx_stream.h
 * @brief      Bounded incremental validation of H102 v1 RX streams.
 * @details    Accept arbitrary chunks; commit complete frames only; fail on corruption without searching for another magic.
 * @author     LogicRF Technologies
 * @architect  LogicRF Technologies
 * @email      info@logicrf.com
 * @website    https://www.logicrf.com
 * @version    1.0.0
 * @date       2026-09-27
 * @note       Laboratory tool; not RF performance or sustained-lossless qualification.
 */

#ifndef H102_USB_RX_STREAM_H
#define H102_USB_RX_STREAM_H
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/** @brief Complete frames per channel; next is the expected next sample index. */
struct h102_rx_channel {
    uint64_t frames, samples, missing, next;
    bool ended;
};
/** @brief Gap callbacks report absolute byte offsets/sample indices, not hardware timestamps. */
typedef void (*h102_gap_fn)(void *, unsigned, uint64_t, uint64_t, uint64_t);
/** @brief Single-thread parser caches only the 32-byte header, without inspecting unknown RF payload values. */
struct h102_rx_stream {
    unsigned mask;
    uint32_t epoch;
    unsigned char header[32];
    size_t header_used, payload_left, payload_size;
    uint64_t offset, frame_offset;
    struct h102_rx_channel channel[2];
    const char *error;
    h102_gap_fn gap;
    void *context;
};
/** @brief Read unaligned little-endian integers. */
static inline uint64_t h102_le(const unsigned char *p, size_t n)
{
    uint64_t v=0;
    for (size_t i=0; i<n; ++i) v |= (uint64_t)p[i] << (8*i);
    return v;
}
/** @brief Validate header/sequence without committing partial-payload statistics. */
static inline bool h102_rx_header(struct h102_rx_stream *s)
{
    const unsigned char *h=s->header;
    uint64_t first=h102_le(h+16,8), n=h102_le(h+8,4);
    if (memcmp(h,"H102",4) || h[4]!=1 || h[5]!=1 || h[6]<1 || h[6]>2 ||
        h[7]&~7u || !n || n>65504 || n%4 || h102_le(h+24,8)) {
        s->error="invalid_header"; return false;
    }
    if (!(s->mask & (1u << (h[6]-1))) || h102_le(h+12,4)!=s->epoch) {
        s->error="mask_or_epoch"; return false;
    }
    const struct h102_rx_channel *c=&s->channel[h[6]-1];
    if (!!(h[7]&1) != !c->frames || c->ended || first<c->next ||
        first>UINT64_MAX-n/4 || !!(h[7]&4)!=(first!=c->next)) {
        s->error="sequence_or_flags"; return false;
    }
    s->payload_size=s->payload_left=(size_t)n;
    return true;
}
/** @brief Commit complete frames and count actual gaps, including a nonzero first-frame index. */
static inline void h102_rx_commit(struct h102_rx_stream *s)
{
    unsigned ch=s->header[6];
    struct h102_rx_channel *c=&s->channel[ch-1];
    uint64_t first=h102_le(s->header+16,8), n=s->payload_size/4;
    if (first!=c->next && s->gap) s->gap(s->context,ch,s->frame_offset,c->next,first);
    c->missing+=first-c->next;
    c->next=first+n;
    c->frames++;
    c->samples+=n;
    c->ended=!!(s->header[7]&2);
    s->header_used=0;
    s->frame_offset=s->offset;
}
/** @brief Consume sequential chunks; true consumes this chunk, false permanently latches the first failure. */
static inline bool h102_rx_feed(struct h102_rx_stream *s, const unsigned char *p, size_t n)
{
    if (s->error) return false;
    while (n) {
        if (s->header_used<32) {
            size_t take=32-s->header_used;
            if (take>n) take=n;
            memcpy(s->header+s->header_used,p,take);
            s->header_used+=take; s->offset+=take; p+=take; n-=take;
            if (s->header_used==32 && !h102_rx_header(s)) return false;
        } else {
            size_t take=s->payload_left<n?s->payload_left:n;
            s->payload_left-=take; s->offset+=take; p+=take; n-=take;
            if (!s->payload_left) h102_rx_commit(s);
        }
    }
    return true;
}
#endif
