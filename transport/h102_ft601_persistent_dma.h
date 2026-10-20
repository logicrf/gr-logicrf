/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_ft601_persistent_dma.h
 * @brief Persistent DMA initialization/lifecycle statistics for finite mixed candidates.
 * @details Pinned SDK candidates only; no RF control or system IOMMU changes.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-11
 * @note Not board-qualified for the release stack.
 */
#ifndef H102_FT601_PERSISTENT_DMA_H
#define H102_FT601_PERSISTENT_DMA_H
#include <stdint.h>
#include <stdio.h>
/** @brief Update counters under mutex; bytes counts mappings still owned by this process. */
struct h102_dma_stats {
    uint64_t allocated,released,submitted,cache_hits,completed,errors,bytes,peak_bytes,active,peak_active;
};
/** @brief Check SDK-exported libusb version; nonzero refuses activation. */
int h102_dma_initialize(void);
/**
 * @brief After SDK stops submitting, cancel adapter in-flight requests and await every SDK callback.
 * @param milliseconds Monotonic wait limit; never free mappings or SDK objects here.
 * @return Zero means quiescent; nonzero forbids FT_Close/application-buffer release and requires process exit/outer recovery.
 * @note One-shot stop rejects later submissions; never call from completion callbacks.
 */
int h102_dma_quiesce(unsigned milliseconds);
/** @brief Return thread-safe statistics without USB access. */
struct h102_dma_stats h102_dma_snapshot(void);
/** @brief Write JSON statistics; caller owns the output file. */
void h102_dma_report(FILE *output);
#endif
