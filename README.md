# gr-logicrf · GNU Radio for the LogicRF Hawk H102

Open a `.grc` flowgraph in GNU Radio Companion, enter the H102 IP address, and receive or transmit IQ over Gigabit Ethernet IIO or a separately deployed USB 3.0 backend. C++ handles the IQ data path; Python handles configuration, bindings and session management. The interface uses GNU Radio Qt GUI.

**Version: 1.2.2rc2 (pre-release).** This package provides the GNU Radio module, examples, control runtime and source installer. It uses the 1.2.1 data path and works with the matching H102 services described below. It does not install or replace board firmware.

## Features

| Feature | Scope and limits |
|---|---|
| IQ receive/transmit | Single/dual RX, single/dual TX and supported mixed combinations; cf32 streams through C++ |
| Configuration | 58 built-in profiles with matching sample rate and bandwidth; switch profiles while stopped |
| RF control | Center frequency, RX gain and TX attenuation; hardware, shared LO and profile constraints checked before acquisition |
| Analysis | Native spectrum, waterfall and I/Q time plots; FM tuning, station search and volume |
| Recording/replay | Recording integrity checks, metadata index and file checksums; replay with original integer time tags |
| Timestamps | USB preserves device digital time and quality; IIO does not synthesize absolute ADC UTC |
| Connection | Device IP only; no Linux username or SSH password; matching services must be installed on the H102 beforehand |

The IQ16 transport budget is 64 MB/s for Ethernet and 320 MB/s for USB. The module checks allowed combinations and rejects excess demand. Meeting the budget does not guarantee lossless operation in every environment. Loss, identity mismatches and recovery failures are reported explicitly; there is no automatic rate reduction or backend substitution.

## Quick start

Verified host combination: Ubuntu 22.04 x86_64, Python 3.10, **GNU Radio 3.10.12.0 with QtGUI/GRC**, and **libiio 0.23 / 0.x ABI**. Windows and other Python/GNU Radio combinations are not qualified. The GNU Radio version in a distribution's package repository may differ.

Prepare dependencies and matching H102 services using the [installation guide](docs/INSTALL.md), then run from the source root:

```bash
bash scripts/install_from_source.sh \
  /path/to/gnuradio-3.10.12.0-prefix \
  "$HOME/LogicRF/hawk-h102/install/gr-logicrf/1.2.2rc2" \
  "$HOME/LogicRF/hawk-h102"

python3 "$HOME/LogicRF/hawk-h102/install/gr-logicrf/1.2.2rc2/launch.py" grc
```

The installer builds, runs offline tests, installs and verifies the bundled control source. Installation does not connect to a device or require a private Git repository. Install GNU Radio and system dependencies first. This source installation does not deploy the USB vendor library or USB host service.

Open an example under the installation's `share/gr-logicrf/examples` directory and save a writable copy:

| Example | Purpose |
|---|---|
| `h102_fm_broadcast.grc` | Single RX FM, spectrum, station search and volume |
| `h102_sdr_receive.grc` / `h102_sdr_single_rx.grc` | Dual/single RX spectrum, waterfall and I/Q time plots |
| `h102_iq_record.grc` / `h102_iq_record_single_rx.grc` | Dual/single RX recording with integrity checks |
| `h102_iq_replay.grc` | Verify a completed recording and replay its original integer time tags to EOF |

Select the device IP, connection type, profile and channels, then generate and run. Click **Start receiving** to begin. Opening or generating a flowgraph does not connect to the H102. Only one application can use the device at a time. Stop reception and disconnect before switching applications. Connect the correct ports, loads and attenuators before transmitting.

## Documentation and licensing

For USB setup, use the separate [USB host deployment tools](docs/USB-HOST-PORTABLE.md). They build and install the matching host helpers and local service for a selected user; vendor inputs remain separate. The module's ordinary source installer does not perform this administrator setup.

- [Installation and matching versions](docs/INSTALL.md)
- [Operation, profiles and integrity](docs/USAGE.md)
- [APIs and programming examples](docs/API.md)
- [Release and verification scope](docs/RELEASE.md)
- [Maintaining the English distribution](docs/MAINTAINING.md)
- [GPL scope](LICENSING.md) and [third-party dependencies](THIRD-PARTY-NOTICES.md)

LogicRF-owned code is licensed under **GPL-3.0-or-later**. The bundled minimal control runtime comes from a pinned source version; see `control-runtime/SOURCE-MAP.json` for provenance and per-file SHA values. Complete RTL, Kuiper images, the standalone GUI and vendor components have separate distribution and licensing arrangements.
