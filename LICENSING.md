# gr-logicrf licensing

SPDX-License-Identifier: GPL-3.0-or-later

Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd.

LogicRF-owned gr-logicrf source, build scripts, tests, examples and documentation in this distribution are provided under the GNU General Public License, version 3 or, at your option, any later version. See [LICENSE](LICENSE) for the full license. This grant does not change the rights or licenses of third-party material.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.

## Scope

- This grant covers LogicRF-owned content in `include`, `lib`, `python`, `transport`, `cmake`, `grc`, `scripts`, `tests`, `examples`, `control-runtime` and the build configuration.
- Historical `All Rights Reserved` source headers retain copyright attribution. The explicit GPL-3.0-or-later grant in this distribution applies to the LogicRF-owned material listed above.
- LogicRF-owned documentation is provided under the same license. Trademarks, third-party excerpts and external dependencies retain their respective rights. This license does not grant permission to use LogicRF trademarks.
- `control-runtime` contains the minimal LogicRF control source included in this module. Its exact origin and any distribution modifications are recorded in `control-runtime/SOURCE-MAP.json`.
- This file does not relicense the complete H102 RTL, Kuiper images, other repositories, or the complete standalone GUI/board runtime.

## Third-party boundaries

See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). FTDI D3XX headers and binaries are not included. Distribution of a USB helper combined with that library requires a separate license compatibility review. Keeping the vendor library external does not complete that review; no linking exception is added here.

The module source baseline is internal tag `v1.2.1`, commit `002451b3c0a2ba470f6cc13d5b93cd6ca7b59cd9`. Exact distribution identity is defined by its delivery Git tag and file hashes. Source packaging checks do not extend hardware qualification; see [release scope](docs/RELEASE.md).
