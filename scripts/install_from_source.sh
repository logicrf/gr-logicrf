#!/usr/bin/env bash
# Isolate source build/registration; arguments are literal paths; no hardware access or system dependency installation.
set -euo pipefail
if [[ $# -ne 3 ]]; then
  echo 'Usage: bash scripts/install_from_source.sh GNURADIO_PREFIX NEW_INSTALL_PREFIX PROJECT_ROOT' >&2
  exit 2
fi
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
gnu_prefix=$(realpath -- "$1")
install_prefix=$(realpath -m -- "$2")
project_root=$(realpath -m -- "$3")
[[ ! -e "$install_prefix" ]] || { echo 'New installation directory required' >&2; exit 2; }
mkdir -p "$project_root/build"
build_root=$(mktemp -d "$project_root/build/gr-logicrf-public-XXXXXXXX")
# CTest captures Python environment during configuration; set it before cmake, not only before ctest.
export PYTHONPATH="$gnu_prefix/lib/python3.10/site-packages:$gnu_prefix/lib/python3/dist-packages:/usr/lib/python3/dist-packages"
export LD_LIBRARY_PATH="$gnu_prefix/lib"
export PATH="$gnu_prefix/bin:$PATH"
export LOGICRF_PROJECT_ROOT="$project_root" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
cmake -S "$source_root" -B "$build_root" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_PREFIX_PATH="$gnu_prefix" \
  -DCMAKE_INSTALL_PREFIX="$install_prefix" -DPython3_EXECUTABLE=/usr/bin/python3 \
  -DLOGICRF_D3XX_ROOT=
cmake --build "$build_root" -j 8
LOGICRF_PROJECT_ROOT="$project_root" \
LD_LIBRARY_PATH="$build_root:$gnu_prefix/lib" \
PYTHONPATH="$build_root/python:$gnu_prefix/lib/python3.10/site-packages:$gnu_prefix/lib/python3/dist-packages" \
ctest --test-dir "$build_root" --output-on-failure
cmake --install "$build_root"
/usr/bin/python3 -B "$source_root/scripts/register_install.py" --prefix "$install_prefix" \
  --gnuradio-prefix "$gnu_prefix" --project-root "$project_root"
