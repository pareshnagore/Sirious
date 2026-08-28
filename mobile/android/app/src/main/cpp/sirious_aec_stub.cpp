// Stub for non-arm64 ABIs. Flutter's Gradle plugin force-adds all default
// ABIs regardless of abiFilters; those builds get this no-op so the APK
// still links. The real AEC3 wrapper is sirious_aec_wrapper.cpp (arm64-v8a).

#include <cstdint>

extern "C" {

void* sirious_aec_create() { return nullptr; }
void sirious_aec_destroy(void*) {}
int sirious_aec_process_render(void*, const int16_t*, int) { return -1; }
int sirious_aec_process_capture(void*, const int16_t*, int, int, int16_t*) { return -1; }
int sirious_aec_delay_valid(void*) { return -1; }

}  // extern "C"
