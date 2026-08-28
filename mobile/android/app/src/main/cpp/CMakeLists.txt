cmake_minimum_required(VERSION 3.22)
project(sirious_aec LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ---- Platform defines (mirrors upstream meson.build for android/aarch64) ----
# Arch defines are ABI-conditional: the ARM64 NEON path is aarch64-only
# (vmull_high_s16 etc. do not exist on armv7). Flutter's Gradle plugin appends
# all default ABIs regardless of abiFilters, so non-arm64 ABIs get a stub.
if(ANDROID_ABI STREQUAL "arm64-v8a")
  add_compile_definitions(
    WEBRTC_ANDROID
    WEBRTC_LINUX
    WEBRTC_POSIX
    WEBRTC_ARCH_ARM64
    WEBRTC_HAS_NEON
    WEBRTC_LIBRARY_IMPL
    WEBRTC_APM_DEBUG_DUMP=0
    NDEBUG
  )
else()
  # Non-arm64 ABIs: build only a stub .so — the SM-E346B is arm64-only and
  # Flutter's Gradle plugin force-adds all default ABIs regardless of
  # abiFilters. Keeps those builds cheap and green.
  add_library(sirious_aec SHARED sirious_aec_stub.cpp)
  target_link_libraries(sirious_aec PRIVATE log)
  return()
endif()

# ---- abseil-cpp (vendored, static) ----
set(ABSL_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(ABSL_BUILD_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory(abseil-cpp EXCLUDE_FROM_ALL)

# ---- webrtc-audio-processing v2.1 (vendored, static) ----
set(WAP ${CMAKE_CURRENT_SOURCE_DIR}/webrtc-audio-processing/webrtc)

file(STRINGS ${CMAKE_CURRENT_SOURCE_DIR}/sources.txt WAP_SOURCES)
list(TRANSFORM WAP_SOURCES PREPEND webrtc-audio-processing/webrtc/)

add_library(webrtc_audio_processing STATIC ${WAP_SOURCES})
target_include_directories(webrtc_audio_processing PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/webrtc-audio-processing/webrtc)

# NEON is implied by -DWEBRTC_HAS_NEON on aarch64 (clang defaults to NEON on arm64)
target_compile_options(webrtc_audio_processing PRIVATE
  -O2 -fno-exceptions -fno-rtti -fvisibility=hidden -Wno-unused-variable
)

target_link_libraries(webrtc_audio_processing PUBLIC
  absl::base absl::flags absl::strings absl::numeric absl::synchronization absl::bad_optional_access
  log
)

# ---- thin C ABI wrapper ----
add_library(sirious_aec SHARED sirious_aec_wrapper.cpp)
target_link_libraries(sirious_aec PRIVATE webrtc_audio_processing)
