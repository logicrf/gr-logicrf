cmake_minimum_required(VERSION 3.20)
project(h102_usb_host_tools VERSION 1.0.0 LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)
set(LOGICRF_D3XX_ROOT "" CACHE PATH "Separately supplied, pinned D3XX 1.1.8 inputs")
foreach(pin
    "libftd3xx.so.1.1.8:949445eb019a6fe3e314e54cc45af1b8ddb1fcdf0c377f1144a59143d0a08752"
    "ftd3xx.h:06ba4fc2d11680f974c1cfa76d504c61ef7325a089133e5247271d25d7111cb4"
    "Types.h:1f1669cea660bddffc9e42197201b2f17dec6c8a81fb2e12b268748d4ce9d66b"
    "libusb.h:e55b50eeedab44f0aa62ce6c2126d47ba80e8d9be1a67a62c27d2349bbf00c02")
  string(REPLACE ":" ";" parts "${pin}")
  list(GET parts 0 name)
  list(GET parts 1 expected)
  file(SHA256 "${LOGICRF_D3XX_ROOT}/${name}" actual)
  if(NOT actual STREQUAL expected)
    message(FATAL_ERROR "External USB input pin mismatch: ${name}")
  endif()
endforeach()
get_filename_component(source_root "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
foreach(direction rx tx duplex)
  add_executable(logicrf-ft601-${direction}
    "${source_root}/transport/ft601_${direction}.cc"
    "${source_root}/transport/h102_ft601_persistent_dma.c")
  target_include_directories(logicrf-ft601-${direction} PRIVATE "${LOGICRF_D3XX_ROOT}")
  target_link_libraries(logicrf-ft601-${direction} PRIVATE
    "${LOGICRF_D3XX_ROOT}/libftd3xx.so.1.1.8" Threads::Threads dl)
  target_compile_options(logicrf-ft601-${direction} PRIVATE -Wall -Wextra)
  set_target_properties(logicrf-ft601-${direction} PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH "$ORIGIN")
endforeach()
configure_file("${LOGICRF_D3XX_ROOT}/libftd3xx.so.1.1.8" "${CMAKE_BINARY_DIR}/libftd3xx.so" COPYONLY)
