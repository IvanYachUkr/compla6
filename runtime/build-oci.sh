#!/bin/sh
# Both stages fail closed; no mutable archive or package-version fallback.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(python3 -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$root/pyproject.toml")
docker build -f "$root/runtime/oci/Dockerfile.toolchain" -t compression-lab-toolchain:20260906 "$root/runtime/oci"
docker build -f "$root/runtime/Dockerfile" --build-arg "COMPRESSION_LAB_VERSION=$version" -t "compression-lab-evaluator:$version" "$root"
docker image inspect "compression-lab-evaluator:$version" --format '{{.Id}}'
