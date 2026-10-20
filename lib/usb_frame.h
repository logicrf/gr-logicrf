/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file usb_frame.h
 * @brief Complete USB v3 frame submission and per-frame native metadata interfaces.
 * @details Single-thread incremental parsing publishes only fully verified configuration/sequence/time frames; callbacks retain no pointers.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.3.0
 * @date 2026-10-16
 * @note IQ is little-endian s16 I/Q; time reference excludes RF-delay calibration.
 ******************************************************************************/
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <gnuradio/logicrf/rx_frame_info.h>
#ifdef __cplusplus
extern "C" {
#endif
/** @brief Return zero to accept a full frame or nonzero for downstream failure; pointers live within callbacks only. */
typedef int (*usb_frame_fn)(void*, const unsigned char*, size_t, const struct usb_frame_metadata*);
struct usb_decoder;
/** @brief Create a decoder from original 112-byte board readback; return NULL on error. */
struct usb_decoder* usb_decoder_new(unsigned mask, uint64_t stream, uint32_t epoch,
    uint64_t rate, uint32_t bandwidth, const char* configuration, usb_frame_fn callback, void* context);
/** @brief Return a static error name, or NULL on success; latch first error without resynchronization. */
const char* usb_decoder_feed(struct usb_decoder*, const unsigned char*, size_t);
/** @brief Uncommitted tail bytes; normal STOP may leave a partial frame, never complete IQ output. */
size_t usb_decoder_partial(const struct usb_decoder*);
/** @brief Verified complete samples/frames per channel; channel must be 1 or 2. */
uint64_t usb_decoder_samples(const struct usb_decoder*, unsigned channel);
uint64_t usb_decoder_frames(const struct usb_decoder*, unsigned channel);
void usb_decoder_free(struct usb_decoder*);
#ifdef __cplusplus
}
#endif
