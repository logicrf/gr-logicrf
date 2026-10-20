/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file       h102_usb_tx_v3.h
 * @brief      Generate 128-byte TX v3 frames and deterministic IQ from frozen configuration.
 * @details    Finite frames, per-channel sequence/sample indices and END; no USB/RF access or timed TX.
 * @author     LogicRF Technologies
 * @architect  LogicRF
 * @email      info@logicrf.com
 * @website    https://www.logicrf.com
 * @version    0.2.0
 * @date       2026-10-10
 * @note       Legacy interface is fixed 40 MSPS/20 MHz; runtime entry requires separately verified actual profile Fs/BW.
 */
#ifndef H102_USB_TX_V3_H
#define H102_USB_TX_V3_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#define H102_TX_SAMPLES 2048u ///< IQ samples per complete frame, independent of USB packet boundaries.
#define H102_TX_FRAME_BYTES 8320u ///< 128-byte header plus 8192-byte IQ.
/** @brief Single-thread generator; next counts generated frames per channel; submission is counted separately. */
struct h102_tx_source {
    unsigned char header[2][128];
    unsigned mask, channel;
    uint32_t seed;
    uint64_t frames, next[2];
};
/** @brief Host-order-independent LE integer read; caller ensures length at most eight bytes. */
static inline uint64_t h102_tx_le(const unsigned char *p,size_t bytes)
{
    uint64_t result=0;
    for(size_t i=0;i<bytes;++i) result|=(uint64_t)p[i]<<(8*i);
    return result;
}
/** @brief LE writes without unaligned-access assumptions. */
static inline void h102_tx_put(unsigned char *p,uint64_t value,size_t bytes)
{
    for(size_t i=0;i<bytes;++i) p[i]=(unsigned char)(value>>(8*i));
}
/** @brief Numeric range matches 13 approved Fs values; valid numbers do not grant hardware/RF qualification. */
static inline bool h102_tx_runtime_profile(uint64_t rate,uint32_t bandwidth)
{
    switch(rate) {
    case 192000: case 768000: case 1536000: case 2000000: case 4000000:
    case 8000000: case 10000000: case 16000000: case 20000000: case 23040000:
    case 30720000: case 40000000: case 61440000:
        return bandwidth && bandwidth<=rate && bandwidth<=40000000;
    default:return false;
    }
}
/** @brief Match independently checked Fs/BW exactly; initial dynamic flags/sequence/indices must be zero. */
static inline bool h102_tx_template_runtime(const unsigned char *h,unsigned channel,
                                            uint64_t rate,uint32_t bandwidth)
{
    if(!h102_tx_runtime_profile(rate,bandwidth)) return false;
    unsigned char canonical[128]={0};
    memcpy(canonical,"H102",4);canonical[4]=3;canonical[5]=2;
    canonical[6]=(unsigned char)channel;canonical[7]=1;canonical[8]=128;
    h102_tx_put(canonical+16,8192,4);h102_tx_put(canonical+48,rate,8);
    h102_tx_put(canonical+56,1,4);h102_tx_put(canonical+60,2048,4);
    h102_tx_put(canonical+72,bandwidth,4);memset(canonical+104,1,6);
    memcpy(canonical+32,h+32,12);memcpy(canonical+64,h+64,8);
    memcpy(canonical+76,h+76,4);memcpy(canonical+96,h+96,8);
    uint64_t lo=h102_tx_le(h+64,8);
    return !memcmp(h,canonical,128) && h102_tx_le(h+32,8) && h102_tx_le(h+40,4) &&
        h102_tx_le(h+96,8) && lo>=30000000 && lo<=UINT64_C(6000000000);
}
/** @brief Preserve legacy 40 MSPS/20 MHz entry; never infer actual hardware parameters from input headers. */
static inline bool h102_tx_template(const unsigned char *h,unsigned channel)
{
    return h102_tx_template_runtime(h,channel,40000000,20000000);
}
/** @brief Check templates/finite limits; channels must share session, configuration and device identity. */
static inline bool h102_tx_initialize_runtime(struct h102_tx_source *s,const unsigned char *templates,
    unsigned mask,uint64_t frames,uint32_t seed,uint64_t rate,uint32_t bandwidth)
{
    if(mask<1 || mask>3 || frames<2 || frames>UINT64_MAX/H102_TX_SAMPLES ||
       !h102_tx_runtime_profile(rate,bandwidth)) return false;
    for(unsigned ch=0;ch<2;++ch)
        if((mask&(1u<<ch)) && !h102_tx_template_runtime(templates+ch*128,ch+1,rate,bandwidth)) return false;
    if(mask==3 && (memcmp(templates+32,templates+160,12) || memcmp(templates+96,templates+224,8))) return false;
    memset(s,0,sizeof(*s));memcpy(s->header,templates,256);
    s->mask=mask;s->channel=mask==2?1:0;s->frames=frames;s->seed=seed;
    return true;
}
/** @brief Legacy fixed-rate generation retains bytes/rejection boundaries; RF tone callers still use it. */
static inline bool h102_tx_initialize(struct h102_tx_source *s,const unsigned char *templates,
                                      unsigned mask,uint64_t frames,uint32_t seed)
{
    return h102_tx_initialize_runtime(s,templates,mask,frames,seed,40000000,20000000);
}
/** @brief Output one complete frame; false means finite generation finished and leaves output untouched. */
static inline bool h102_tx_next(struct h102_tx_source *s,unsigned char *output)
{
    unsigned ch=s->channel;
    if(s->next[ch]>=s->frames) return false;
    uint64_t frame=s->next[ch],index=frame*H102_TX_SAMPLES;
    memcpy(output,s->header[ch],128);
    output[10]=frame==0;output[11]=frame+1==s->frames;
    h102_tx_put(output+20,(uint32_t)frame,4);h102_tx_put(output+24,index,8);
    uint32_t channel_seed=s->seed^(ch?UINT32_C(0x6d2b79f5):0);
    uint32_t value=channel_seed+(uint32_t)index*UINT32_C(0x9e3779b9);
    for(unsigned i=0;i<H102_TX_SAMPLES;++i) {
        h102_tx_put(output+128+4*i,value^UINT32_C(0xc0000000),4);
        value+=UINT32_C(0x9e3779b9);
    }
    ++s->next[ch];
    if(s->mask==3) s->channel^=1;
    return true;
}
#endif
/* End of file: h102_usb_tx_v3.h */
