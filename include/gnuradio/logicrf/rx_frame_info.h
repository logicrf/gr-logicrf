/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file rx_frame_info.h
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
#include <stdint.h>
/** @brief Native identity/integer rational time at first frame sample; not proof of cross-channel alignment. */
struct usb_frame_metadata {
    uint64_t first, anchor_seconds, stream_id, device_id, rate_num, center_hz;
    uint32_t samples, sequence, config_id, time_epoch, clock_hz, offset_ticks, rate_den, bandwidth_hz;
    int32_t gain_mdb;
    uint8_t channel, time_valid, gain_mode;
};
