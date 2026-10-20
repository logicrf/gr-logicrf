/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file qa_device.cc
 * @brief Check shared-queue chunks, channel identity, stop and strict overflow contracts.
 * @details Hardware-free mocks never qualify IIO/USB/RF; Release builds still execute checks.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-16
 * @note Throw and exit nonzero on failure; do not use assertions removable by NDEBUG.
 ******************************************************************************/
#include <gnuradio/logicrf/device.h>
#include <chrono>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>
using namespace gr::logicrf;

/** @brief Retain exact failed contract names and check them in Release builds too. */
static void require(bool value, const char* message)
{
    if (!value) throw std::runtime_error(message);
}
/** @brief Require the expected error and matching cause. */
template <class Fn> static void rejects(Fn fn, const std::string& reason)
{
    try { fn(); } catch (const std::exception& error) {
        require(std::string(error.what()).find(reason) != std::string::npos, error.what()); return;
    }
    throw std::runtime_error("expected rejection: " + reason);
}
int main()
{
    try {
        device_options options;
        options.frame_samples = 257; options.mock_samples = 12345;
        options.sample_rate = 100000;
        auto board = device::make(options);
        board->attach(1); board->attach(2);
        rejects([&] { board->attach(1); }, "BUSY");
        board->enter(1); board->enter(2);
        rejects([&] { board->set_center_frequency(200e6); }, "STOP_REQUIRED");
        std::uint64_t totals[2] = {0, 0};
        bool done[2] = {false, false};
        std::complex<float> samples[101];
        while (!done[0] || !done[1]) {
            for (unsigned channel = 1; channel <= 2; ++channel) {
                if (done[channel - 1]) continue;
                std::uint64_t index = 0;
                auto n = board->read(channel, samples, 101, index);
                require(n != -2, "unexpected overflow in paced capture");
                if (n == -1) { done[channel - 1] = true; continue; }
                if (!n) continue;
                require(index == totals[channel - 1], "fragment index discontinuity");
                for (std::ptrdiff_t j = 0; j < n; ++j) {
                    int code = static_cast<int>((index + j + (channel - 1) * 97) & 65535);
                    require(samples[j].real() == float(code - 32768) / 32768.0f, "IQ16 normalization/channel mismatch");
                    require(samples[j].imag() == float(32767 - code) / 32768.0f, "Q sign mismatch");
                }
                totals[channel - 1] += n;
            }
        }
        require(totals[0] == 12345 && totals[1] == 12345, "finite tail lost");
        board->leave(1); board->leave(2);
        board->set_center_frequency(200e6);
        board->enter(1); board->enter(2);
        board->leave(1); board->leave(2);
        require(board->error().empty(), "restart failed");
        std::cout << "PASS dual-channel-fragments-normalization-restart\n";

        options.mock_samples = 0; options.queue_frames = 1; options.frame_samples = 64;
        options.sample_rate = 1e6;
        auto stalled = device::make(options);
        stalled->attach(1); stalled->enter(1);
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        std::uint64_t index = 0;
        require(stalled->read(1, samples, 101, index) == -2, "overflow was not explicit");
        require(stalled->error().find("OVERFLOW") != std::string::npos, "overflow reason missing");
        auto start = std::chrono::steady_clock::now();
        stalled->leave(1);
        require(std::chrono::steady_clock::now() - start < std::chrono::milliseconds(250), "stop cancellation exceeded bound");
        std::cout << "PASS strict-overflow-and-cancellation\n";

        options.backend = "iio"; options.sample_rate = 20e6;
        auto hardware = device::make(options);
        hardware->attach(1); hardware->attach(2);
        rejects([&] { hardware->enter(1); }, "BUDGET_EXCEEDED");
        options.backend = "usb";
        auto usb = device::make(options); usb->attach(1);
        rejects([&] { usb->enter(1); }, "MANAGED_SESSION_REQUIRED");
        options.center_frequency = std::nan("");
        rejects([&] { device::make(options); }, "invalid");
        std::cout << "PASS budget-unsupported-backend-invalid-input\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL " << error.what() << '\n'; return 1;
    }
}
