/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file fm_audio_sink.h
 * @brief Native PulseAudio output and underflow statistics for mono float FM audio.
 * @details Connect at start, not construction; C++ worker follows service backpressure; stop drains within bounds.
 *          No H102 access, implicit ALSA fallback or system-volume change.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 1.1.0
 * @date 2026-10-17
 * @note Linux PulseAudio/PipeWire-compatible services only; no stereo or RF qualification.
 ******************************************************************************/
#pragma once
#include <gnuradio/sync_block.h>
#include <cstdint>
#include <map>
#include <string>
namespace gr::logicrf {
/** @brief One 48 kHz float output; GNU serializes start/stop and counters support concurrent reads. */
class fm_audio_sink : virtual public gr::sync_block {
public:
    using sptr=std::shared_ptr<fm_audio_sink>;
    /** @brief latency_ms ranges from 100 to 2000; service refusal/disconnect fails without input drops. */
    static sptr make(unsigned latency_ms=1000);
    /** @brief Current/latest audio counts; underflows exclude pre-write and explicit-drain periods. */
    virtual std::map<std::string,std::uint64_t> snapshot() const=0;
    /** @brief Latest audio-service error; empty means none recorded, alongside sample/underflow counters. */
    virtual std::string error() const=0;
    /** @brief Explicitly discard unplayed audio on stop/tune; thread-safe, with PA calls confined to the audio thread.
     * @details Preserve IQ input protection; subsequent input is consumed and counted as discarded_input_samples.
     *          Default EOF still drains unless requested; start resets the request.
     */
    virtual void discard_pending()=0;
};
} // namespace gr::logicrf
