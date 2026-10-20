/******************************************************************************
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file module.cc
 * @brief GNU Radio bindings for static Device options and single-channel RX Sources.
 * @details C++ workers handle data completely; Python construction never accesses hardware.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.2.0
 * @date 2026-10-16
 * @note pybind11 shared-pointer inheritance matches GNU Radio 3.10.
 ******************************************************************************/
#include <gnuradio/logicrf/device.h>
#include <gnuradio/logicrf/fm_audio_sink.h>
#include <gnuradio/logicrf/h102_metadata_sink.h>
#include <gnuradio/logicrf/h102_replay_tagger.h>
#include <gnuradio/logicrf/h102_rx_source.h>
#include <gnuradio/logicrf/h102_tx_sink.h>
#include <gnuradio/logicrf/tx_ramp_source.h>
#include <pybind11/pybind11.h>
#include <pybind11/functional.h>
#include <pybind11/stl.h>
#include <pybind11/complex.h>
#include <gnuradio/logicrf/stream_probe.h>
namespace py = pybind11;
using namespace gr::logicrf;
/** @brief Release the interpreter lock during blocking join and restore it on the original thread. */
struct interpreter_release {
    PyThreadState* state = nullptr;
    interpreter_release() { if (PyGILState_Check()) state = PyEval_SaveThread(); }
    ~interpreter_release() { if (state) PyEval_RestoreThread(state); }
};
PYBIND11_MODULE(logicrf_python, module)
{
    py::module_::import("gnuradio.gr");
    module.doc() = "LogicRF H102 OOT: managed IIO/USB RX, finite mock TX, RF-OFF IIO finite submission and USB digital RAMP32 TX candidates; RF TX is not implemented";
    py::class_<h102_replay_tagger,gr::sync_block,gr::block,gr::basic_block,h102_replay_tagger::sptr>(module,"h102_replay_tagger")
        .def(py::init(&h102_replay_tagger::make))
        .def("configure",&h102_replay_tagger::configure,py::arg("index"),py::arg("backend"),
             py::arg("rate"),py::arg("frequency"),py::arg("configuration"),py::arg("boot_id")="",py::arg("active_info_json")="{}")
        .def("error",&h102_replay_tagger::error);
    py::class_<fm_audio_sink, gr::sync_block, gr::block, gr::basic_block, fm_audio_sink::sptr>(module,"fm_audio_sink")
        .def(py::init(&fm_audio_sink::make),py::arg("latency_ms")=1000)
        .def("discard_pending",&fm_audio_sink::discard_pending)
        .def("snapshot",&fm_audio_sink::snapshot)
        .def("error",&fm_audio_sink::error);
    py::class_<device_options>(module, "device_options")
        .def(py::init<>())
        .def_readwrite("direction", &device_options::direction)
        .def_readwrite("tx_samples", &device_options::tx_samples)
        .def_readwrite("tx_validation", &device_options::tx_validation)
        .def_readwrite("usb_tx_templates", &device_options::usb_tx_templates)
        .def_readwrite("usb_tx_seed", &device_options::usb_tx_seed)
        .def_readwrite("usb_socket", &device_options::usb_socket)
        .def_readwrite("usb_configuration", &device_options::usb_configuration)
        .def_readwrite("usb_stream_id", &device_options::usb_stream_id)
        .def_readwrite("usb_time_epoch", &device_options::usb_time_epoch)
        .def_readwrite("backend", &device_options::backend)
        .def_readwrite("uri", &device_options::uri)
        .def_readwrite("configuration", &device_options::configuration)
        .def_readwrite("sample_rate", &device_options::sample_rate)
        .def_readwrite("center_frequency", &device_options::center_frequency)
        .def_readwrite("frame_samples", &device_options::frame_samples)
        .def_readwrite("queue_frames", &device_options::queue_frames)
        .def_readwrite("mock_samples", &device_options::mock_samples)
        .def_readwrite("sample_limit", &device_options::sample_limit)
        .def_readwrite("rf_bandwidth", &device_options::rf_bandwidth)
        .def_readwrite("boot_id", &device_options::boot_id)
        .def_readwrite("metadata_json", &device_options::metadata_json);
    py::class_<device, device::sptr>(module, "device")
        .def_static("make", [](const device_options& options) {
            auto value = device::make(options);
            value->set_runtime_unlock([] {
                return std::static_pointer_cast<void>(std::make_shared<interpreter_release>());
            });
            return value;
        }, py::arg("options"))
        .def("options", &device::options)
        .def("set_center_frequency", &device::set_center_frequency)
        .def("error", &device::error)
        .def("produced", &device::produced)
        .def("received", &device::received)
        .def("queue_stats", &device::queue_stats)
        .def("tx_stats", &device::tx_stats)
        .def("tx_input_done", &device::tx_input_done)
        .def("confirm_tx_playing", &device::confirm_tx_playing)
        .def("request_tx_stop", &device::request_tx_stop)
        .def("request_rx_stop", &device::request_rx_stop)
        .def("set_frame_samples", &device::set_frame_samples)
        .def("finish_tx_input", &device::finish_tx_input)
        .def("ready", &device::ready)
        .def("renew_lease", &device::renew_lease)
        .def("invalidate", &device::invalidate)
        .def("configure", &device::configure)
        .def("shutdown", &device::shutdown, py::call_guard<py::gil_scoped_release>())
        .def("set_session", &device::set_session);
    py::class_<stream_probe, gr::sync_block, gr::block, gr::basic_block, stream_probe::sptr>(module,"stream_probe")
        .def(py::init(&stream_probe::make),py::arg("ramp16")=false)
        .def("snapshot",&stream_probe::snapshot)
        .def("prefix",&stream_probe::prefix)
        .def("signal_snapshot",&stream_probe::signal_snapshot)
        .def("metadata",&stream_probe::metadata)
        .def("tags",&stream_probe::tags);
    py::class_<h102_rx_source, gr::sync_block, gr::block, gr::basic_block,
               h102_rx_source::sptr>(module, "h102_rx_source")
        .def(py::init(&h102_rx_source::make), py::arg("board"), py::arg("channel"));
    py::class_<h102_tx_sink, gr::sync_block, gr::block, gr::basic_block,
               h102_tx_sink::sptr>(module, "h102_tx_sink")
        .def(py::init(&h102_tx_sink::make), py::arg("board"), py::arg("channel"));
    py::class_<tx_ramp_source, gr::sync_block, gr::block, gr::basic_block,
               tx_ramp_source::sptr>(module,"tx_ramp_source")
        .def(py::init(&tx_ramp_source::make),py::arg("board"),py::arg("channel"));
    py::class_<h102_metadata_sink,gr::sync_block,gr::block,gr::basic_block,h102_metadata_sink::sptr>(module,"h102_metadata_sink")
        .def(py::init(&h102_metadata_sink::make),py::arg("board"),py::arg("path"),py::arg("channel"))
        .def("set_path",&h102_metadata_sink::set_path)
        .def("records",&h102_metadata_sink::records).def("error",&h102_metadata_sink::error);
}
