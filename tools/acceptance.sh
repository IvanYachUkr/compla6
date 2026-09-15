#!/bin/sh
# Usage: tools/acceptance.sh /absolute/new-output-directory [--matrix]
# Default: one native Zstandard/Hadoop control through ALL full gates.
# --matrix: the longer three-family, nine-control package CLI demo.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${1:?Provide a new acceptance output directory}"
out=$1;shift
venv=${COMPRESSION_LAB_VENV:-"$root/.acceptance-venv"}
version=$(python3 -I -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$root/pyproject.toml")
python3 -I -m venv "$venv"
"$venv/bin/python" -I -m pip install --no-index --no-deps "$root/dist/compression_lab-$version-py3-none-any.whl"
"$venv/bin/python" -I -m pip check
if [ "$#" -eq 0 ]; then
 exec "$venv/bin/python" -I "$root/tools/acceptance_demo.py" --output "$out"
elif [ "$#" -eq 1 ] && [ "$1" = "--matrix" ]; then
 exec "$venv/bin/python" -I "$venv/bin/compression-lab" demo --output "$out" --matrix
else
 echo 'Only the optional --matrix flag is supported.' >&2
 exit 2
fi
