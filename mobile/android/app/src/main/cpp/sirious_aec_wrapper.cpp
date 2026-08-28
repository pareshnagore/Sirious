// Thin C ABI wrapper around webrtc-audio-processing v2.1 (AEC3) for dart:ffi.
//
// Frame contract: 10 ms interleaved int16 frames.
//   capture: mic leg  -> process_capture()  [after set delay]
//   render : playback -> process_render()  (far-end reference)
//
// Threads: process_render() is called from the playback path and
// process_capture() from the capture path. The upstream contract forbids
// concurrent stream calls, so everything funnels through one mutex.

#include <atomic>
#include <mutex>

#include "api/audio/audio_processing.h"

namespace {

constexpr int kCaptureRate = 16000;  // record plugin capture rate
constexpr int kRenderRate = 24000;   // flutter_pcm_sound playout rate
constexpr int kChannels = 1;

struct AecHandle {
  rtc::scoped_refptr<webrtc::AudioProcessing> apm;
  std::mutex mutex;
};

}  // namespace

extern "C" {

// Returns opaque handle or nullptr.
void* sirious_aec_create() {
  webrtc::AudioProcessing::Config config;
  config.echo_canceller.enabled = true;  // AEC3 in v2.x
  config.echo_canceller.mobile_mode = false;
  config.high_pass_filter.enabled = true;
  config.noise_suppression.enabled = true;
  config.noise_suppression.level =
      webrtc::AudioProcessing::Config::NoiseSuppression::kModerate;

  auto apm = webrtc::AudioProcessingBuilder().Create();
  if (!apm) {
    return nullptr;
  }
  apm->ApplyConfig(config);

  auto* h = new AecHandle();
  h->apm = apm;
  return h;
}

void sirious_aec_destroy(void* handle) {
  if (!handle) {
    return;
  }
  auto* h = static_cast<AecHandle*>(handle);
  std::lock_guard<std::mutex> lock(h->mutex);
  h->apm = nullptr;  // releases the scoped_refptr
  delete h;
}

// Feed one 10 ms frame of playback audio (far end / render).
// samples = samples_per_channel * channels interleaved.
int sirious_aec_process_render(void* handle, const int16_t* data,
                               int samples_per_channel) {
  auto* h = static_cast<AecHandle*>(handle);
  if (!h || !h->apm) {
    return -1;
  }
  const webrtc::StreamConfig cfg(kRenderRate, kChannels);
  std::lock_guard<std::mutex> lock(h->mutex);
  return h->apm->ProcessReverseStream(
      data, cfg, cfg, const_cast<int16_t*>(data));
}

// Feed one 10 ms frame of mic audio (near end) and get the echo-cancelled
// output. delay_ms = (playback time) - (capture time) for the SAME physical
// audio, i.e. how much later the mic hears what the speaker played.
int sirious_aec_process_capture(void* handle, const int16_t* data,
                                int samples_per_channel, int delay_ms,
                                int16_t* out) {
  auto* h = static_cast<AecHandle*>(handle);
  if (!h || !h->apm) {
    return -1;
  }
  const webrtc::StreamConfig cfg(kCaptureRate, kChannels);
  std::lock_guard<std::mutex> lock(h->mutex);
  h->apm->set_stream_delay_ms(delay_ms);
  return h->apm->ProcessStream(data, cfg, cfg, out);
}

// Returns 1 when the AEC's delay estimator has a delay measurement
// (kill-switch signal: sustained 0 here = AEC3 latency tracking diverged
// on this device), 0 when it doesn't, -1 on error.
int sirious_aec_delay_valid(void* handle) {
  auto* h = static_cast<AecHandle*>(handle);
  if (!h || !h->apm) {
    return -1;
  }
  std::lock_guard<std::mutex> lock(h->mutex);
  auto stats = h->apm->GetStatistics(/*has_remote_tracks=*/true);
  return stats.delay_ms.has_value() ? 1 : 0;
}

}  // extern "C"
