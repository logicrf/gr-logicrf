# H102 GNU Radio examples

Start Companion with the installation's `launch.py grc`, open a `.grc` file and save a writable copy. Enter the actual device IP; `192.0.2.10` is a documentation placeholder. Ordinary use does not require an SSH password. An administrator must first install matching services.

| Flowgraph | Purpose |
|---|---|
| h102_fm_broadcast.grc | Single RX FM, tuning/search, spectrum and volume |
| h102_sdr_receive.grc / h102_sdr_single_rx.grc | Dual/single RX spectrum, waterfall and I/Q time plots |
| h102_iq_record.grc / h102_iq_record_single_rx.grc | Dual/single RX strict recording and integrity status |
| h102_iq_replay.grc | SHA verification and integer tag replay of completed recordings; no hardware access |
| mock_dual_rx.grc / mock_dual_tx.grc | Hardware-free build and installation checks |

Other CLI and RX/TX flowgraphs demonstrate lower-level combinations. `native` demonstrates CMake/C++ package consumption. Profiles, sample rates and channels must agree; connect every selected channel to the flowgraph.

Generating a flowgraph does not connect to a device. Starting acquisition requests ownership. Connect the correct load/attenuation before RF transmission.

See [operation](../docs/USAGE.md) for construction, stop and recovery boundaries, [installation](../docs/INSTALL.md) for matching versions and [release scope](../docs/RELEASE.md) for qualification limits.
