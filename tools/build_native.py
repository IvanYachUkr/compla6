#!/usr/bin/env python3
"""Build the morning pinned native codec closure in an isolated prefix.

The script is deliberately self-contained and resumable.  It downloads only
the URLs in native-lock.json, builds static archives with at most two jobs,
copies headers/licenses into the commissioned prefix, then compiles and runs a
version/API probe against each produced archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve()
STAGE = SCRIPT.parents[1]
LOCK = STAGE / "dependencies" / "native-lock.json"
# Keep defaults anchored to this repository stage, rather than to the current
# working directory or an installed package.
REPO = SCRIPT.parents[5]
NATIVE = REPO / "handover" / "morning-2026-09-07" / "native"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_inventory(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run(command: list[str], cwd: Path, log: Path, env: dict[str, str] | None = None) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    rendered = " ".join(command)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"\n$ {rendered}\n")
        stream.flush()
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}; see {log}")
    return ""


def download(url: str, target: Path, log: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20", "--output", str(target), url],
        cwd=target.parent,
        log=log,
    )


def verify_archive(pkg: str, spec: dict, archive: Path, receipt_dir: Path) -> dict:
    observed = {"sha256": sha256(archive), "size_bytes": archive.stat().st_size}
    if spec.get("source_archive_sha256"):
        if observed["sha256"] != spec["source_archive_sha256"]:
            raise RuntimeError(f"{pkg}: SHA-256 mismatch for {archive.name}")
        observed["sha256_verified_against"] = spec["source_hash_url"]
    if spec.get("source_archive_sha512"):
        observed["sha512"] = sha512(archive)
        if observed["sha512"] != spec["source_archive_sha512"]:
            raise RuntimeError(f"{pkg}: SHA-512 mismatch for {archive.name}")
        observed["sha512_verified_against"] = spec["source_hash_url"]
    if not spec.get("source_archive_sha256") and not spec.get("source_archive_sha512"):
        sidecar = receipt_dir / f"{pkg}.archive.sha256"
        if sidecar.exists() and sidecar.read_text(encoding="utf-8").strip() != observed["sha256"]:
            raise RuntimeError(f"{pkg}: unverified archive changed since its first receipt")
        sidecar.write_text(observed["sha256"] + "\n", encoding="utf-8")
        observed["sha256_verified_against"] = "local sidecar plus immutable source pin"
    return observed


def safe_extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            candidate = (destination / member.name).resolve()
            if candidate != destination.resolve() and destination.resolve() not in candidate.parents:
                raise RuntimeError(f"archive path escapes extraction directory: {member.name}")
            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                if link_target.is_absolute():
                    raise RuntimeError(f"archive link is absolute: {member.name}")
                resolved_target = (candidate.parent / link_target).resolve()
                if resolved_target != destination.resolve() and destination.resolve() not in resolved_target.parents:
                    raise RuntimeError(f"archive link escapes extraction directory: {member.name} -> {member.linkname}")
        tar.extractall(destination, filter="data")
    roots = sorted(path for path in destination.iterdir() if path.name != ".DS_Store")
    if len(roots) == 1 and roots[0].is_dir():
        return roots[0]
    return destination


def copy_headers(source: Path, prefix: Path, names: list[str]) -> list[str]:
    copied = []
    for name in names:
        candidates = [source / name, *source.rglob(Path(name).name)]
        source_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source_path is None:
            raise RuntimeError(f"header not found in source: {name}")
        if "/" in name:
            target = prefix / "include" / name
        else:
            target = prefix / "include" / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        copied.append(str(target.relative_to(prefix)))
    return copied


def copy_static_libs(build_root: Path, prefix: Path, names: list[str]) -> list[str]:
    copied = []
    for name in names:
        candidates = sorted(path for path in build_root.rglob(name) if path.is_file())
        if not candidates:
            raise RuntimeError(f"static library not found after build: {name}")
        target = prefix / "lib" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidates[-1], target)
        copied.append(str(target.relative_to(prefix)))
    return copied


def copy_licenses(pkg: str, source: Path, prefix: Path) -> list[str]:
    candidates = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.stat().st_size == 0 or path.stat().st_size > 2 * 1024 * 1024:
            continue
        upper = path.name.upper()
        if upper in {"LICENSE", "LICENSE.TXT", "COPYING", "COPYING.TXT", "COPYING.LIB", "LICENSE.md".upper()} or upper.startswith("COPYING."):
            candidates.append(path)
    if not candidates:
        raise RuntimeError(f"no license file found for {pkg}")
    # Build systems may generate empty license placeholders (zstd's lib build
    # creates build/LICENSE). Prefer the shortest non-empty source path for a
    # duplicate filename so the shipped license is actual upstream text.
    selected: dict[str, Path] = {}
    for candidate in candidates:
        previous = selected.get(candidate.name)
        if previous is None or len(candidate.parts) < len(previous.parts):
            selected[candidate.name] = candidate
    target_dir = prefix / "share" / "licenses" / pkg
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for source_path in selected.values():
        target = target_dir / source_path.name
        if not target.exists() or target.read_bytes() != source_path.read_bytes():
            shutil.copy2(source_path, target)
        outputs.append(str(target.relative_to(prefix)))
    return outputs


PROBES: dict[str, tuple[str, list[str]]] = {
    "zstd": (
        r'''#include <stdio.h>
#include <string.h>
#include "zstd.h"
int main(void) {
  const char *s = "native-zstd-probe"; char out[256]; char back[256];
  ZSTD_CCtx *ctx = ZSTD_createCCtx();
  if (!ctx || ZSTD_isError(ZSTD_CCtx_setParameter(ctx, ZSTD_c_nbWorkers, 1))) return 2;
  size_t n = ZSTD_compress2(ctx, out, sizeof out, s, strlen(s));
  size_t m = ZSTD_decompress(back, sizeof back, out, n);
  ZSTD_freeCCtx(ctx);
  if (ZSTD_isError(n) || ZSTD_isError(m) || m != strlen(s) || memcmp(back, s, m)) return 3;
  printf("package=zstd version=%s number=%u roundtrip=ok mt_parameter=ok\n", ZSTD_versionString(), ZSTD_versionNumber());
  return 0;
}
''',
        ["zstd", "pthread"],
    ),
    "lz4": (
        r'''#include <stdio.h>
#include <string.h>
#include "lz4.h"
int main(void) {
  const char *s = "native-lz4-probe"; char out[256]; char back[256];
  int n = LZ4_compress_default(s, out, (int)strlen(s), sizeof out);
  int m = LZ4_decompress_safe(out, back, n, sizeof back);
  if (n <= 0 || m != (int)strlen(s) || memcmp(back, s, m)) return 2;
  printf("package=lz4 version=%s number=%d roundtrip=ok\n", LZ4_versionString(), LZ4_versionNumber());
  return 0;
}
''',
        ["lz4"],
    ),
    "brotli": (
        r'''#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "brotli/encode.h"
#include "brotli/decode.h"
int main(void) {
  const uint8_t s[] = "native-brotli-probe"; uint8_t out[256]; uint8_t back[256];
  size_t n = sizeof out, m = sizeof back;
  if (!BrotliEncoderCompress(5, 22, BROTLI_MODE_GENERIC, sizeof(s) - 1, s, &n, out)) return 2;
  if (BrotliDecoderDecompress(n, out, &m, back) != BROTLI_DECODER_RESULT_SUCCESS) return 3;
  if (m != sizeof(s) - 1 || memcmp(back, s, m)) return 4;
  printf("package=brotli version_number=0x%08x decoder_number=0x%08x roundtrip=ok\n", BrotliEncoderVersion(), BrotliDecoderVersion());
  return 0;
}
''',
        ["brotlienc", "brotlidec", "brotlicommon"],
    ),
    "xz": (
        r'''#include <stdio.h>
#include <string.h>
#include <lzma.h>
int main(void) {
  const uint8_t s[] = "native-xz-probe"; uint8_t out[512]; uint8_t back[256];
  size_t n = 0, m = 0;
  if (lzma_easy_buffer_encode(6, LZMA_CHECK_CRC64, NULL, s, sizeof(s) - 1, out, &n, sizeof out) != LZMA_OK) return 2;
  uint64_t memlimit = UINT64_MAX; uint64_t in_pos = 0, out_pos = 0;
  if (lzma_stream_buffer_decode(&memlimit, 0, NULL, out, &in_pos, n, back, &out_pos, sizeof back) != LZMA_OK) return 3;
  if (out_pos != sizeof(s) - 1 || memcmp(back, s, out_pos)) return 4;
  printf("package=xz version=%s number=%u roundtrip=ok\n", lzma_version_string(), lzma_version_number());
  return 0;
}
''',
        ["lzma"],
    ),
    "zlib": (
        r'''#include <stdio.h>
#include <string.h>
#include <zlib.h>
int main(void) {
  const Bytef s[] = "native-zlib-probe"; Bytef out[256]; Bytef back[256];
  uLong n = sizeof out, m = sizeof back;
  if (compress(out, &n, s, sizeof(s) - 1) != Z_OK || uncompress(back, &m, out, n) != Z_OK) return 2;
  if (m != sizeof(s) - 1 || memcmp(back, s, m)) return 3;
  printf("package=zlib version=%s flags=%lu roundtrip=ok\n", zlibVersion(), zlibCompileFlags());
  return 0;
}
''',
        ["z"],
    ),
    "bzip2": (
        r'''#include <stdio.h>
#include <string.h>
#include "bzlib.h"
int main(void) {
  char s[] = "native-bzip2-probe"; char out[256]; char back[256];
  unsigned int n = sizeof out, m = sizeof back;
  if (BZ2_bzBuffToBuffCompress(out, &n, s, (unsigned)strlen(s), 9, 0, 30) != BZ_OK) return 2;
  if (BZ2_bzBuffToBuffDecompress(back, &m, out, n, 0, 0) != BZ_OK) return 3;
  if (m != strlen(s) || memcmp(back, s, m)) return 4;
  printf("package=bzip2 version=%s roundtrip=ok\n", BZ2_bzlibVersion());
  return 0;
}
''',
        ["bz2"],
    ),
}


def build_package(pkg: str, spec: dict, source: Path, build: Path, prefix: Path, log: Path, cflags: list[str], cc: str, jobs: int) -> dict:
    env = os.environ.copy()
    env.update({"CC": cc, "AR": "ar", "RANLIB": "ranlib", "CFLAGS": " ".join(cflags)})
    build.mkdir(parents=True, exist_ok=True)
    if pkg == "zstd":
        # The static library is single-threaded by default in this release;
        # the explicit lib-mt target is required for ZSTD_c_nbWorkers.
        run(["make", "-C", "lib", f"-j{jobs}", "lib-mt", f"CC={cc}", f"CFLAGS={' '.join(cflags)}"], source, log, env)
        libs = copy_static_libs(source / "lib", prefix, spec["static_libraries"])
        headers = copy_headers(source, prefix, spec["headers"])
    elif pkg == "lz4":
        run(["make", "-C", "lib", f"-j{jobs}", "liblz4.a", f"CC={cc}", f"CFLAGS={' '.join(cflags)}"], source, log, env)
        libs = copy_static_libs(source / "lib", prefix, spec["static_libraries"])
        headers = copy_headers(source / "lib", prefix, spec["headers"])
    elif pkg == "brotli":
        cmake_build = build / "cmake"
        run(["cmake", "-S", str(source), "-B", str(cmake_build), "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DBROTLI_BUILD_TOOLS=OFF", "-DBROTLI_BUILD_TESTS=OFF", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", f"-DCMAKE_C_COMPILER={cc}", f"-DCMAKE_C_FLAGS={' '.join(cflags)}"], source, log, env)
        run(["cmake", "--build", str(cmake_build), f"-j{jobs}"], source, log, env)
        libs = copy_static_libs(cmake_build, prefix, spec["static_libraries"])
        headers = copy_headers(source / "c", prefix, spec["headers"])
    elif pkg == "xz":
        configure = ["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static", "--disable-xz", "--disable-xzdec", "--disable-lzmadec", "--disable-lzmainfo", "--disable-scripts", "--disable-doc", f"CC={cc}", f"CFLAGS={' '.join(cflags)}"]
        run(configure, source, log, env)
        run(["make", f"-j{jobs}"], source, log, env)
        run(["make", "install"], source, log, env)
        libs = ["lib/liblzma.a"]
        # The umbrella API header is installed beside the child headers. Keep
        # it in the receipt explicitly instead of relying on the child-header
        # glob, because resume validation must account for this required asset.
        umbrella = prefix / "include" / "lzma.h"
        source_umbrella = source / "src" / "liblzma" / "api" / "lzma.h"
        if not umbrella.is_file() or not source_umbrella.is_file() or sha256(umbrella) != sha256(source_umbrella):
            raise RuntimeError("xz: installed umbrella header does not match pinned source")
        headers = ["include/lzma.h"] + [str(path.relative_to(prefix)) for path in sorted((prefix / "include" / "lzma").rglob("*.h"))]
    elif pkg == "zlib":
        run(["./configure", "--static", f"--prefix={prefix}", f"CC={cc}", f"CFLAGS={' '.join(cflags)}"], source, log, env)
        run(["make", f"-j{jobs}"], source, log, env)
        run(["make", "install"], source, log, env)
        libs = ["lib/libz.a"]
        headers = ["include/zlib.h", "include/zconf.h"]
    elif pkg == "bzip2":
        run(["make", f"-j{jobs}", "libbz2.a", f"CC={cc}", f"AR=ar", f"RANLIB=ranlib", f"CFLAGS={' '.join(cflags)}"], source, log, env)
        libs = copy_static_libs(source, prefix, spec["static_libraries"])
        headers = copy_headers(source, prefix, spec["headers"])
    else:
        raise RuntimeError(f"unsupported package: {pkg}")
    licenses = copy_licenses(pkg, source, prefix)
    return {"static_libraries": libs, "headers": headers, "licenses": licenses}


def probe_package(pkg: str, spec: dict, prefix: Path, build: Path, log: Path, cc: str, cflags: list[str]) -> dict:
    source, libs = PROBES[pkg]
    build.mkdir(parents=True, exist_ok=True)
    probe_source = build / f"probe_{pkg}.c"
    probe_exe = build / f"probe_{pkg}"
    probe_source.write_text(source, encoding="utf-8")
    command = [cc, *cflags, "-I", str(prefix / "include"), str(probe_source), "-L", str(prefix / "lib"), "-Wl,-Bstatic"]
    command.extend(f"-l{name}" for name in libs)
    command.extend(["-Wl,-Bdynamic", "-o", str(probe_exe)])
    if pkg == "brotli":
        command.append("-lm")
    if pkg in {"zstd", "xz"}:
        command.append("-pthread")
    run(command, build, log)
    result = subprocess.run([str(probe_exe)], cwd=build, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{pkg}: API probe failed ({result.returncode}): {result.stderr.strip()}")
    output = result.stdout.strip()
    expected = spec["probe_expected_version"]
    if pkg == "brotli":
        if spec["probe_expected_version_number"] not in output:
            raise RuntimeError(f"{pkg}: API probe version mismatch: {output}")
    elif f"version={expected}" not in output:
        raise RuntimeError(f"{pkg}: API probe version mismatch: {output}")
    return {"command": command, "executable": str(probe_exe), "stdout": output, "returncode": 0}


def prior_outputs_valid(prior: dict, prefix: Path, spec: dict) -> bool:
    outputs = prior.get("outputs", {})
    if not outputs:
        return False
    expected = {f"lib/{name}" for name in spec.get("static_libraries", [])}
    expected.update(f"include/{name}" for name in spec.get("headers", []))
    if not expected.issubset(outputs):
        return False
    for relative, expected_digest in outputs.items():
        path = prefix / relative
        if not path.is_file() or sha256(path) != expected_digest:
            return False
        if relative.startswith("share/licenses/") and path.stat().st_size == 0:
            return False
    for header in spec.get("headers", []):
        if not (prefix / "include" / header).is_file():
            return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--download-dir", type=Path, default=NATIVE / "downloads")
    parser.add_argument("--build-dir", type=Path, default=NATIVE / "build")
    parser.add_argument("--prefix", type=Path, default=NATIVE / "prefix")
    parser.add_argument("--receipt-dir", type=Path, default=NATIVE / "receipts")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--cc", default="gcc")
    parser.add_argument("--package", choices=["zstd", "lz4", "brotli", "xz", "zlib", "bzip2"], action="append")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1 or args.jobs > 2:
        raise SystemExit("--jobs must be between 1 and 2")
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    for path in (args.download_dir, args.build_dir, args.prefix, args.receipt_dir):
        path.mkdir(parents=True, exist_ok=True)
    cflags = lock["compiler"]["cflags"]
    packages = args.package or list(lock["packages"])
    receipt_path = args.receipt_dir / "native-build-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {
        "schema_version": 1,
        "status": "running",
        "lock_path": str(args.lock),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "compiler": {},
        "packages": {},
    }
    cc_path = shutil.which(args.cc)
    if not cc_path:
        raise SystemExit(f"compiler not found: {args.cc}")
    cc_version = subprocess.run([args.cc, "--version"], text=True, capture_output=True, check=True).stdout.splitlines()[0]
    receipt["compiler"] = {"cc": cc_path, "version": cc_version, "flags": cflags, "jobs": args.jobs, "host": platform.platform()}
    for pkg in packages:
        spec = lock["packages"][pkg]
        log = args.receipt_dir / "logs" / f"{pkg}.log"
        archive = args.download_dir / spec["source_archive"]
        source = args.build_dir / pkg / "source"
        pkg_build = args.build_dir / pkg
        package_receipt = args.receipt_dir / f"{pkg}.json"
        if package_receipt.exists():
            prior = json.loads(package_receipt.read_text(encoding="utf-8"))
            if prior.get("status") == "verified" and prior_outputs_valid(prior, args.prefix, spec):
                receipt["packages"][pkg] = prior
                continue
        download(spec["source_url"], archive, log)
        archive_observed = verify_archive(pkg, spec, archive, args.receipt_dir)
        if not source.exists():
            source = safe_extract(archive, pkg_build / "extract")
        built = build_package(pkg, spec, source, pkg_build, args.prefix, log, cflags, args.cc, args.jobs)
        probe = probe_package(pkg, spec, args.prefix, pkg_build / "probes", log, args.cc, cflags)
        output_files = {path: sha256(args.prefix / path) for path in [*built["static_libraries"], *built["headers"], *built["licenses"]]}
        item = {
            "status": "verified",
            "version": spec["version"],
            "release_date": spec["release_date"],
            "release_url": spec["release_url"],
            "source_url": spec["source_url"],
            "immutable_git_commit": spec.get("immutable_git_commit"),
            "archive": {"path": str(archive), **archive_observed},
            "source_tree": {"path": str(source), "sha256_files": file_inventory(source)},
            "outputs": output_files,
            "probe": probe,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        package_receipt.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["packages"][pkg] = item
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["prefix"] = {"path": str(args.prefix), "sha256_files": file_inventory(args.prefix)}
    receipt["status"] = "verified"
    receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path), "prefix": str(args.prefix), "packages": packages}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
