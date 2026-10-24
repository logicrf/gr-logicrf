# Install the USB host backend

USB host tools **1.0.0-r1** install the FT601 receive/transmit helpers and local control service for one ordinary login user. The supported reference host is **Ubuntu 22.04 x86_64 with Python 3.10**. Configure the username, project directory and external D3XX location for that computer. Routine GNU Radio connections require only the H102 IP; administrator authentication is used during installation and maintenance.

Connect USB to the computer running the flowgraph. Device control still uses Ethernet and matching H102 API36/capability-3 services. The standalone IIO GUI uses Ethernet IQ. The existing USB service accepts one FT601 at a time and rejects ambiguous device identification.

These tools retain the existing receive/transmit implementation. They do not install board firmware or add RF, timestamp-accuracy, host-platform or arbitrary-duration recording qualification. Installation and service readiness are separate from a successful data test.

## Prepare the external inputs

Obtain the following matching D3XX 1.1.8 inputs separately. Neither this source package nor a prepared LogicRF payload includes these vendor files. The tools do not download them or accept vendor agreements on your behalf.

Choose **Linux x64 1.1.8** on the [official FTDI D3XX driver page](https://ftdichip.com/drivers/d3xx-drivers/) and extract the shared library, `ftd3xx.h` and `Types.h`. Obtain the fourth file, `libusb.h`, from [the official libusb v1.0.27 source](https://raw.githubusercontent.com/libusb/libusb/v1.0.27/libusb/libusb.h) and save it in the same input directory. Verify all four hashes below; a different system header is not a substitute.

| File | SHA-256 |
| --- | --- |
| `libftd3xx.so.1.1.8` | `949445eb019a6fe3e314e54cc45af1b8ddb1fcdf0c377f1144a59143d0a08752` |
| `ftd3xx.h` | `06ba4fc2d11680f974c1cfa76d504c61ef7325a089133e5247271d25d7111cb4` |
| `Types.h` | `1f1669cea660bddffc9e42197201b2f17dec6c8a81fb2e12b268748d4ce9d66b` |
| `libusb.h` | `e55b50eeedab44f0aa62ce6c2126d47ba80e8d9be1a67a62c27d2349bbf00c02` |

The host needs systemd, `fuser` from `psmisc`, a C/C++ compiler, CMake 3.20 or later and Ninja. An administrator can install the build prerequisites on Ubuntu:

```bash
sudo apt install build-essential cmake ninja-build psmisc
```

## Build and prepare a payload as the application user

Run from the extracted source root. Set the paths for your host. Use new build and bundle directories; keep an existing installation intact.

```bash
PROJECT="$HOME/LogicRF/hawk-h102"
D3XX="$HOME/LogicRF/vendor/ftd3xx/1.1.8"
BUILD="$PROJECT/build/usb-host/1.0.0-r1"
BUNDLE="$PROJECT/releases/usb-host-1.0.0-r1"
mkdir -p "$PROJECT/build/usb-host" "$PROJECT/releases" "$PROJECT/envs/gr-usb"
chmod 700 "$PROJECT/envs/gr-usb"
cmake -S tools/usb-host -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DLOGICRF_D3XX_ROOT="$D3XX"
cmake --build "$BUILD" -j8
python3 -B scripts/install_usb_host_portable.py prepare --build-root "$BUILD" --output "$BUNDLE"
```

The independent helper build does not require GNU Radio. Preparation freezes the helper binaries and their LogicRF source files, then prints `manifest_sha256`. Keep that exact hash for the administrator. A different build may produce different binaries and requires its own reviewed payload. A prepared bundle can be rebuilt using `source/tools/usb-host` and the same external inputs.

Keep the project path short enough for Unix sockets: the socket directory plus its generated filename must fit within 107 bytes. An unsupported path is rejected before installation.

## Stage a new installation as an administrator

Review the source and prepared manifest first. Replace the approval placeholder below with the exact printed SHA-256. `--user` selects the existing ordinary login account that will run GNU Radio. It does not create an account. Root and service accounts are rejected.

```bash
LOGIN_USER="$(id -un)"
INSTALL="/opt/logicrf/hawk-h102/usb-host/1.0.0-r1"
APPROVED_MANIFEST_SHA=REPLACE_WITH_THE_VERIFIED_MANIFEST_SHA256
sudo python3 -B scripts/install_usb_host_portable.py install \
  --bundle "$BUNDLE" --manifest-sha256 "$APPROVED_MANIFEST_SHA" \
  --user "$LOGIN_USER" --project-root "$PROJECT" --install-root "$INSTALL" \
  --d3xx-library "$D3XX/libftd3xx.so.1.1.8"
```

The installation directory must be new and have root-owned ancestors without external write access. The application user must already own the private `envs/gr-usb` directory created above. `STAGED_NOT_ACTIVATED` confirms checked files were staged; it leaves the current service selected.

On a host with a recognized existing installation, the tools preserve its user, original lock path and inode. An original private lock may belong to root or the registered application user; do not change its owner to bypass a check. If only a legacy lock exists without the service, select that exact original lock using `--lock-file`. Never delete a lock or create a parallel lock to clear an error.

## Activate the service

Stop reception, recording and transmission, disconnect all clients and wait for device release. Connect the FT601 at SuperSpeed and confirm it is idle. On a host with an existing service, read its current unit hash and supply it explicitly:

```bash
sudo sha256sum /etc/systemd/system/logicrf-h102-usb-api.service
sudo python3 -B scripts/install_usb_host_portable.py activate \
  --install-root "$INSTALL" --previous-unit-sha256 REPLACE_WITH_CURRENT_UNIT_SHA256
systemctl is-active logicrf-h102-usb-api.service
```

Omit `--previous-unit-sha256` only on a new host where that service does not exist. Activation checks the original lock, USB idleness, service syntax and unchanged previous unit. It waits for the selected user's private control socket before returning `ACTIVE_NOT_DATA_QUALIFIED`.

A new host with **no FT601 attached** may use explicit `--offline` activation to prepare the service in advance. This option is rejected when an FT601 is present; it cannot bypass an occupied device or an unsuitable USB link.

After connecting USB, use the matching GNU Radio installation to check bounded reception, recording counts and checksums, playback and normal device release. A running service or a negotiated 5000 Mbps link alone does not establish successful IQ transfer.

## Roll back or remove the service selection

Stop and disconnect clients first, then run:

```bash
sudo python3 -B scripts/install_usb_host_portable.py rollback --install-root "$INSTALL"
```

Rollback verifies that the selected unit still belongs to this activation, then restores the previous unit and its enabled/running state. On a new host, it disables and removes the service created by these tools. Use `--offline` only for a new host with no FT601 attached.

Version files, the original lock and installation/recovery records are retained. Other installations, vendor inputs and recorded IQ are not removed. Each installation directory records one activation transaction; use a new directory for a subsequent installation rather than overwriting its history.

If the unit, code, binaries or lock identity changes, preserve the diagnostic output and investigate. Do not edit checksums or repeatedly reinstall to bypass the error. `FAILED_RESTORE_REQUIRED` or `ROLLBACK_FAILED_RESTORE_REQUIRED` requires administrator recovery before further use.
