# Third-party dependencies and distribution boundaries

This file records dependencies. It does not replace their licenses or grant redistribution rights for vendor material.

| Component | Use | Bundled here? | Status |
|---|---|---|---|
| GNU Radio 3.10.12 | C++ links to GNU Radio runtime; Python uses its runtime | No | Upstream GPLv3 or later; this module's own code is GPL-3.0-or-later |
| libiio 0.x | Linked through pkg-config; requires >=0.23 and <1.0 | No | Preserve the installed version's upstream license and copyright notices; review each component before binary packaging |
| PulseAudio, pybind11, Python, NumPy, Paramiko, Qt/PyQt | Build/runtime dependencies depending on the example and backend | No | Their licenses remain separate; these dependencies are not packaged here |
| FTDI D3XX 1.1.8 | Optional USB helpers link to `libftd3xx.so.1.1.8`; external headers/library have pinned SHA values | No | Vendor/combined distribution review remains open; this list does not authorize public USB binary distribution |
| Minimal LogicRF control runtime | `h102-runtime.lock.json` pins 14 bundled source files and their hashes | Yes, LogicRF-owned source | Included under GPL-3.0-or-later; excludes the complete standalone GUI; provenance is in SOURCE-MAP |
| H102 board firmware, control services and SDK | Accessed through fixed network APIs/device paths; lock files check identity | No | Corresponding source and redistribution obligations are managed separately; this module's license does not replace them |

## Build and delivery requirements

1. The default source preparation builds Ethernet/general core support with `-DLOGICRF_D3XX_ROOT=`.
2. Do not commit vendor SDKs, driver binaries, images, private keys, credentials or captured IQ.
3. Distributed binaries must identify exact source, build scripts and dependency licenses. An old binary package is not evidence that GPL source delivery is complete.
4. A successful source build alone does not establish independent control-runtime installation or extend hardware verification.

References: [GNU Radio FAQ](https://wiki.gnuradio.org/index.php/FAQ), [GNU GPLv3](https://www.gnu.org/licenses/gpl.en.html), [GNU licensing FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#GPLIncompatibleLibs).
