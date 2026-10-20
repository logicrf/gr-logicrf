# APIs and programming examples

## Python / GRC

Import with `from gnuradio import logicrf`. Ordinary connections specify only `uri='ip:192.0.2.10'`, replacing the example IP with the actual device address. Constructing `make_device` does not contact hardware. Flowgraph lifecycle and panel actions trigger actual access.

| Interface | Purpose | Definition |
|---|---|---|
| `make_device` | Static device plan and managed mock/IIO/USB ownership | `python/logicrf/__init__.py` |
| `h102_rx_source` / `h102_tx_sink` | IQ source/sink for a selected channel | Same module and `include/gnuradio/logicrf/` |
| `configuration_catalog` | Offline profile catalog | `python/logicrf/control.py` |
| `device_status` / `select_configuration` | Current plan/latest state and stopped-state profile selection | Same module |
| `dispatch` / `h102_control` | Explicit control requests and PMT replies | Same module; import `dispatch` from `control` |
| `grc_session` / `grc_panel` | Asynchronous GRC session and native Qt panel | `python/logicrf/grc_session.py`, `grc_panel.py` |
| `h102_metadata_sink` / `h102_replay_tagger` | Integrity index and integer tag restoration | Public C++ headers and bindings |

Check Python bindings and the native data path without hardware:

```python
from gnuradio import gr, blocks, logicrf

device = logicrf.make_device(backend='mock', sample_rate=100000, mock_samples=12345)
graph = gr.top_block()
source = logicrf.h102_rx_source(device, 1)
sink = blocks.null_sink(gr.sizeof_gr_complex)
graph.connect(source, sink)
graph.run()
assert sink.nitems_read(0) == 12345 and not device.error()
```

Use complete scripts or `.grc` flowgraphs in `examples` for hardware, TX, mixed sessions and recording. RF transmission is not part of import, construction or installation checks. The matching version's source, headers and block definitions define parameters, types and errors.

## C++

Consume the installed CMake package using `find_package(gr-logicrf CONFIG REQUIRED)`, the exported `logicrf::gnuradio-logicrf` target and public headers under `include/gnuradio/logicrf`. Complete examples are in `examples/native`.

C++ handles IQ processing; Python/API code prepares board hardware and manages sessions. The native data blocks are not a complete native C++ RF control SDK.

## Lower-level device API

`python/h102_control` provides the fixed device API client and session adapter. Ordinary connections use host/port only. There is no arbitrary remote command interface or automatic SSH fallback. An incompatible board version produces an error.

The full H102 IIO attribute reference, profile generation, board SDK and administrator deployment instructions belong to the firmware documentation. This module's interface list does not claim to cover every hardware control interface.
