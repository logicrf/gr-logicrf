/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file       h102_ft601_completion.h
 * @brief      Pinned D3XX completion-event synchronization.
 * @details    Await event publication before reading lengths; Internal completion alone is insufficient.
 * @author     LogicRF Technologies
 * @architect  LogicRF Technologies
 * @email      info@logicrf.com
 * @website    https://www.logicrf.com
 * @version    1.0.0
 * @date       2026-09-27
 * @note       Laboratory tool; not RF performance or sustained-lossless qualification.
 */
#ifndef H102_FT601_COMPLETION_H
#define H102_FT601_COMPLETION_H
#include "ftd3xx.h"
/** @brief Event APIs exported by the pinned Linux D3XX library; build scripts pin ABI and SHA256. */
extern DWORD FT_W32_WaitForSingleObject(HANDLE event, DWORD milliseconds);

/**
 * @brief Poll completion events nonblockingly before reading published byte counts.
 * @details Pinned 1.1.8 writes Internal first, InternalHigh length next, then SetEvent.
 *          GetOverlappedResult(FALSE/TRUE) alone can observe completion with stale length.
 *          Event mutex synchronization establishes length publication; cancellation cannot release slots early.
 * @note Positive timeout on 1.1.8 auto-events may clear simultaneously arriving signals; use zero here only.
 *       Yield independently after incomplete returns without consuming events or releasing slots.
 */
static FT_STATUS completed_read(FT_HANDLE handle, OVERLAPPED *operation, ULONG *count)
{
    *count = 0;
    DWORD event_status = FT_W32_WaitForSingleObject(operation->hEvent, 0);
    if (event_status == WAIT_TIMEOUT) return FT_IO_INCOMPLETE;
    if (event_status != WAIT_OBJECT_0) return FT_OTHER_ERROR;
    return FT_GetOverlappedResult(handle, operation, count, FALSE);
}

#endif
