# Matching H102 system

gr-logicrf 1.2.3 requires the integrated Kuiper 1.2.1 image, whose accepted internal build is 1.2.1-rc2. Use the exact firmware metadata, SDK and API helper identities recorded in the lock files. Replacing only a BIT file or editing a lock to bypass a mismatch is insufficient. IP-only device control is already included in this SD image.

Open a supplied `.grc` flowgraph in GNU Radio Companion, enter the device IP, select an admitted configuration and channels, and run. Normal use requires no device Linux password. USB additionally requires the separate USB host setup.

S0 is the approximately 1 Hz FPGA heartbeat. S1 flashes at approximately 4 Hz during host IQ activity and is off when idle. These indicators do not prove RF power, Linux readiness, GNSS lock or recording integrity.

The exact-image checks cover complete SD write/direct readback, current first-unit cold and warm boot, independent persistent identity, 618 files/five links/three private directories and restoration of all 41 default settings and 12 sources. The ordinary rc3 package recorded dual IIO RX at 4 MSPS, exactly 4,000,000 samples and 32 MB per channel with full-file hashes; the matching USB package completed digital dual TX at 18,726,912 samples per channel with socket/SSI agreement, zero discard and RF off. Version 1.2.3 changes the version label and installation metadata; C++ IQ algorithms are unchanged. It does not extend full-profile endurance, RF, absolute UTC or second-unit qualification. Preserve earlier reports under their original versions.
