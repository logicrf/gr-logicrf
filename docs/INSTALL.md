# Installation and matching versions

## Host dependencies

The reference environment is Ubuntu 22.04 x86_64 / Python 3.10 with GNU Radio 3.10.12.0 including GRC and QtGUI. Use the [official GNU Radio build instructions](https://wiki.gnuradio.org/index.php/InstallingGRFromSource) for installation to a separate user prefix. This repository does not include GNU Radio or its system dependencies. Other package-repository versions are not assumed to be qualified.

Example system prerequisites, installed once by an administrator:

```bash
sudo apt install build-essential cmake ninja-build pkg-config python3-dev \
  pybind11-dev libiio-dev libpulse-dev python3-numpy python3-paramiko \
  python3-pyqt5 python3-gi python3-gi-cairo gir1.2-gtk-3.0
```

These packages do not replace GNU Radio itself. Check the installed versions:

```bash
/path/to/gnuradio-prefix/bin/gnuradio-config-info --version
pkg-config --modversion libiio libpulse
python3 --version
```

Requirements: GNU Radio 3.10.12.0, libiio >=0.23 and <1.0, libpulse >=15, Python 3.10. PySide6, pyqtgraph and the standalone IIO GUI are not required; this module uses GNU Radio/PyQt5 panels.

For normal operation, enter only the device IP. Linux account credentials are used for administrator setup, not for connecting from the GNU Radio examples.

## Install from source

Run the three-argument command in README.md from the source root. The new installation directory must not exist. Build output remains under `build/gr-logicrf-public-*` in the supplied project workspace; failures do not overwrite an earlier installation. The default build parallelism is 8.

The installer writes user directories only. It does not change shell profiles, system Python paths or system dynamic linker configuration. Start with `launch.py`; automatic installation does not alter application menus. To add a desktop entry, use the manual sequence below and add `--desktop-dir "$HOME/.local/share/applications"` to the first registration command. The registrar rejects an already registered installation. Do not register again after automatic installation.

Equivalent manual sequence:

```bash
export PYTHONPATH=/path/to/gnuradio-prefix/lib/python3.10/site-packages:/path/to/gnuradio-prefix/lib/python3/dist-packages:/usr/lib/python3/dist-packages
export LD_LIBRARY_PATH=/path/to/gnuradio-prefix/lib
cmake -S . -B /path/to/new-build -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_PREFIX_PATH=/path/to/gnuradio-prefix \
  -DCMAKE_INSTALL_PREFIX=/path/to/new-install \
  -DPython3_EXECUTABLE=/usr/bin/python3 -DLOGICRF_D3XX_ROOT=
cmake --build /path/to/new-build -j8
LOGICRF_PROJECT_ROOT=/path/to/customer-workspace ctest --test-dir /path/to/new-build --output-on-failure
cmake --install /path/to/new-build
python3 -B scripts/register_install.py --prefix /path/to/new-install \
  --gnuradio-prefix /path/to/gnuradio-prefix --project-root /path/to/customer-workspace
```

Tests require matching GNU Radio Python and library paths; the installer sets these. Model tests explicitly skip external-input checks when vendor SDK inputs are absent. A skip does not qualify USB hardware.

## Matching H102 services

The current data path uses Kuiper/RTL 1.2.0 with API36/cap3 (SDK108) and matching host/board services. High-rate IIO qualification additionally depends on network-performance 1.0.0. An older base image does not imply that these add-ons are installed.

Service versions and source locks must match. Do not edit locks to bypass an error. Administrator deployment and rollback are supplied separately with the firmware delivery. Ordinary operation requires only the IP address, without an SSH/sudo password. A device missing matching services reports a version/connection error. Installing this host source does not modify the board system.

## USB

Follow [Install the USB host backend](USB-HOST-PORTABLE.md) for the separate parameterized deployment tools. Its helper build does not require GNU Radio. Install matching board services and the GNU Radio module separately; routine operation uses the device IP after administrator setup.

The module includes USB protocol source, but the source installation command does not build or distribute FTDI D3XX helpers. USB applications also need an approved matching USB host service, an FT601 connection and network control of the device. Review vendor libraries, headers and combined redistribution terms separately; the GPL grant here does not license vendor material. See [third-party notices](../THIRD-PARTY-NOTICES.md).

## Update and removal

Use a new installation prefix for each update. Stop the old application before selecting the new launcher. `installation.json` records exact paths; reinstall after moving an installation. Before removal, check the receipt and remove only that installation's exclusive directory. Preserve the GNU Radio prefix, workspace evidence and other installed versions.
