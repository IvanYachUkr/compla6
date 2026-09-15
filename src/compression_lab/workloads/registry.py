"""Immutable mutable-candidate registration, reusing 0.1.0 native builders.

Python is source-executed with a copied, hash-pinned interpreter and explicit
stdlib extension closure. There is no free ambient Python runtime or site path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import candidate as native
from ..util import Error, canonical, digest, ident, load, safe, save, sha, secure_load, secure_names

MANIFEST = "store_candidate.json"
BASE_KEYS = {"schema_version", "workload", "protocol_version", "candidate_id", "language",
             "threads", "deterministic", "source_paths", "artifact_paths", "runtime_paths",
             "command", "durability_claim", "training", "python_runtime", "native_build"}
DEFAULT_EXTENSIONS = ("_struct", "binascii", "_hashlib", "fcntl", "select", "_json", "math")


def _closure(paths: list[Path], limits=None) -> dict:
    native.check_limits(limits)
    known = native.libraries(limits)
    todo = list(paths)
    seen: set[str] = set()
    rows = []
    interpreter = None
    while todo:
        native.check_limits(limits)
        info = native.elf(todo.pop())
        native.check_limits(limits)
        interpreter = info["interpreter"] or interpreter
        for name in info["needed"]:
            if name in seen:
                continue
            seen.add(name)
            if name not in known:
                raise Error("missing_language_runtime_dependency", name)
            p = known[name]
            rows.append({"soname": name, "path": str(p), "sha256": native.checked_sha(p, limits),
                         "bytes": p.stat().st_size, "platform": name in native.PLATFORM,
                         "version": "exact file SHA-256 pin", "license": "system distribution runtime; operator supplies license notices"})
            todo.append(p)
    if interpreter:
        if interpreter not in ("/lib64/ld-linux-x86-64.so.2", "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"):
            raise Error("untrusted_elf_interpreter")
        p = Path(interpreter).resolve()
        if str(p) not in [r["path"] for r in rows]:
            rows.append({"soname": p.name, "path": str(p), "sha256": native.checked_sha(p, limits),
                         "bytes": p.stat().st_size, "platform": True,
                         "version": "exact file SHA-256 pin", "license": "LGPL"})
    return {"libraries": sorted(rows, key=lambda x: x["soname"]), "interpreter": interpreter}


def python_runtime_spec(extensions=DEFAULT_EXTENSIONS, limits=None) -> dict:
    native.check_limits(limits)
    if (not isinstance(extensions, (list, tuple)) or len(extensions) > 64
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in extensions)):
        raise Error("invalid_python_extension_list")
    # Never execute an interpreter selected from an agent-owned directory.
    exe = Path(shutil.which("python3", path="/usr/bin:/bin") or "/usr/bin/python3").resolve()
    code = "import sys,os,json,sysconfig;print(json.dumps({'version':sys.version,'minor':str(sys.version_info[0])+'.'+str(sys.version_info[1]),'stdlib':sysconfig.get_path('stdlib'),'builtins':list(sys.builtin_module_names)}))"
    try:
        run = subprocess.run([str(exe), "-I", "-S", "-c", code], capture_output=True, check=True, timeout=native.execution_timeout(limits, 5),
                             env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except subprocess.TimeoutExpired as error:
        if limits is None: raise
        raise Error('timeout') from error
    native.check_limits(limits)
    info = json.loads(run.stdout)
    stdlib = Path(info["stdlib"]).resolve()
    if not str(stdlib).startswith("/usr/lib/python") or info["minor"].split(".")[0] != "3":
        raise Error("unsupported_python_runtime_layout", str(stdlib))
    prefix = "python/lib/python" + info["minor"]
    files = []

    def add(source: Path, target: str):
        native.check_limits(limits)
        source = source.resolve()
        files.append({"path": target, "source": str(source), "bytes": source.stat().st_size,
                      "sha256": native.checked_sha(source, limits), "executable": source == exe})

    add(exe, "python/bin/python" + info["minor"])
    excluded = {"__pycache__", "test", "tests", "site-packages", "dist-packages", "idlelib", "tkinter", "ensurepip"}
    for p in sorted(stdlib.rglob("*.py")):
        native.check_limits(limits)
        relative = p.relative_to(stdlib)
        if any(part in excluded for part in relative.parts) or p.name in ("sitecustomize.py", "usercustomize.py"):
            continue
        if not p.resolve().is_relative_to(stdlib):
            raise Error("python_stdlib_external_symlink", str(p))
        add(p, prefix + "/" + relative.as_posix())
    dynamic = []
    for name in sorted(set(extensions)):
        if name in info["builtins"]:
            continue
        hits = list((stdlib / "lib-dynload").glob(name + ".*.so")) + list((stdlib / "lib-dynload").glob(name + ".so"))
        if len(hits) != 1:
            raise Error("python_extension_unavailable", name)
        p = hits[0]
        dynamic.append(p)
        add(p, prefix + "/lib-dynload/" + p.name)
    notice = Path("/usr/share/doc/python" + info["minor"] + "/copyright")
    if notice.is_file():
        add(notice, "python/LICENSE.distribution")
    dep = _closure([exe, *dynamic], limits)
    out = {"policy": "python-source-runtime-v1", "version": info["version"], "minor": info["minor"],
           "executable": "python/bin/python" + info["minor"], "extension_modules": sorted(set(extensions)),
           "stdlib_policy": "All .py except tests, third-party, GUI, ensurepip and customization; only declared dynamic extensions",
           "files": sorted(files, key=lambda x: x["path"]), "dependencies": dep}
    out["runtime_digest"] = digest(out)
    return out


def native_manifest(m: dict) -> dict:
    """Use the existing native recipe validator, without claiming static I/O."""
    b = m["native_build"]
    if set(b) != {"build_commands", "sanitizer_build_commands", "build_output_paths", "executable", "toolchain", "dependencies"}:
        raise Error("invalid_mutable_native_recipe")
    equivalent = {"schema_version": 1, "candidate_id": m["candidate_id"], "language": m["language"],
                  "input_domain": "opaque_bytes", "deterministic": True, "threads": 1,
                  "source_paths": m["source_paths"], "artifact_paths": m["artifact_paths"],
                  "runtime_paths": m["runtime_paths"], **b}
    equivalent["commands"] = {k: ["{runtime}/" + b["executable"]] for k in
                               ("encode_dir", "decode_dir", "encode_stream", "decode_stream")}
    return native.manifest(equivalent)


def validate(m: dict) -> dict:
    required = BASE_KEYS - {"python_runtime", "native_build", "training"}
    if not isinstance(m, dict) or set(m) - BASE_KEYS or not required <= set(m):
        raise Error("invalid_mutable_manifest")
    if (type(m["schema_version"]) is not int or m["schema_version"] != 1
            or m["workload"] != "mutable_store" or type(m["protocol_version"]) is not int
            or m["protocol_version"] != 1 or type(m["threads"]) is not int or m["threads"] != 1
            or m["deterministic"] is not True or m["language"] not in ("python", "c", "c++")):
        raise Error("invalid_mutable_contract")
    ident(m["candidate_id"])
    if m["durability_claim"] != "process_crash_only":
        raise Error("unsupported_durability_claim", "Power-loss certification is not implemented")
    all_paths = []
    for key in ("source_paths", "artifact_paths", "runtime_paths"):
        if not isinstance(m[key], list) or any(not isinstance(p, str) for p in m[key]):
            raise Error("invalid_path_list")
        for p in m[key]:
            native.rel(p)
        all_paths.extend(m[key])
    if len(all_paths) != len(set(all_paths)) or MANIFEST in all_paths:
        raise Error("mutable_path_overlap")
    cmd = m["command"]
    if not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) or "\0" in x for x in cmd):
        raise Error("invalid_mutable_command")
    if m["language"] == "python":
        if "native_build" in m or not isinstance(m.get("python_runtime"), dict):
            raise Error("missing_declared_python_runtime")
        if (len(cmd) < 5 or cmd[:4] != ["{python}", "-I", "-S", "-B"]
                or not cmd[4].startswith("{runtime}/") or cmd[4][10:] not in m["source_paths"]):
            raise Error("unsafe_python_command", "Use {python} -I -S -B {runtime}/entry.py")
        p = m["python_runtime"]
        if digest({k: v for k, v in p.items() if k != "runtime_digest"}) != p.get("runtime_digest"):
            raise Error("invalid_python_runtime_lock")
    else:
        if "python_runtime" in m or "native_build" not in m:
            raise Error("missing_mutable_native_recipe")
        b = native_manifest(m)
        if cmd[0] != "{runtime}/" + b["executable"]:
            raise Error("unsafe_mutable_native_command")
    return m


def source_package(src: Path, m: dict, out: Path, limits=None) -> dict:
    names = [MANIFEST, *m["source_paths"]]
    native.write_source_archive(src, names, out, limits)
    return {"rule": "zip-deflate-9-v1, fixed timestamp/mode, sorted paths", "bytes": out.stat().st_size, "sha256": native.checked_sha(out, limits)}


def register(root: Path, path: Path, mode="required", limits=None, train_hashes=()) -> dict:
    limits = limits or {}
    native.check_limits(limits)
    if mode != "required":
        raise Error("mutable_isolation_required", "Mutable registration never uses the unsandboxed exploratory build fallback")
    root, path = Path(root), Path(path)
    actual = secure_names(path)
    m = validate(secure_load(path, MANIFEST))
    native.check_limits(limits)
    names = [MANIFEST, *m["source_paths"], *m["artifact_paths"], *m["runtime_paths"]]
    if actual != set(names):
        raise Error('undeclared_or_missing_files')
    if m["artifact_paths"]:
        training = m.get("training", {})
        if training.get("kind") == "train-only":
            hashes = set(training.get("object_sha256", []))
            if not hashes or not hashes <= set(train_hashes):
                raise Error("training_split_violation")
        elif training.get("kind") != "data-independent":
            raise Error("missing_artifact_provenance")
    parent = root / "candidates"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".store-register-", dir=parent) as td:
        tmp = Path(td)
        src, runtime = tmp / "source", tmp / "runtime"
        src.mkdir()
        runtime.mkdir()
        for name in names:
            out = src / name
            out.parent.mkdir(parents=True, exist_ok=True)
            native.copy_import(path, name, out, limits)
        save(src / MANIFEST, m, 0o444)
        source_files = native.scan(src, names, limits)
        program_names = list(m["source_paths"]) if m["language"] == "python" else []
        build_files, logs = [], []
        if m["language"] == "python":
            expected = python_runtime_spec(m["python_runtime"]["extension_modules"], limits)
            if expected != m["python_runtime"]:
                raise Error("python_runtime_pin_mismatch")
            for f in expected["files"]:
                out = runtime / f["path"]
                out.parent.mkdir(parents=True, exist_ok=True)
                native.checked_copy(f["source"], out, limits)
                out.chmod(0o555 if f["executable"] else 0o444)
            dependencies = expected["dependencies"]
            build_key = digest({"program": source_files, "python": expected["runtime_digest"],
                                "protocol": 1, "workload": "mutable_store", "build": "source-executed-no-bytecode"})
        else:
            nm = native_manifest(m)
            built = native.build(src, nm, tmp / "build", mode, limits=limits, cancel=limits.get('_cancel'))
            build_files, dependencies, logs = built["files"], built["dependencies"], built["logs"]
            program_names = nm["build_output_paths"]
            for f in build_files:
                out = runtime / f["path"]
                out.parent.mkdir(parents=True, exist_ok=True)
                native.checked_copy(tmp / "build" / f["path"], out, limits)
                out.chmod(0o555 if f["path"] == nm["executable"] else 0o444)
            shutil.rmtree(tmp / "build")
            build_key = digest({"program": source_files, "recipe": m["native_build"], "build_files": build_files,
                                "dependencies": dependencies, "protocol": 1, "workload": "mutable_store"})
        for name in set((m["source_paths"] if m["language"] == "python" else []) + m["artifact_paths"] + m["runtime_paths"]):
            out = runtime / name
            if out.exists() or name.startswith(("python/", "lib/")):
                raise Error("mutable_runtime_path_collision", name)
            out.parent.mkdir(parents=True, exist_ok=True)
            native.checked_copy(safe(src, name), out, limits)
            out.chmod(0o444)
        (runtime / "lib").mkdir(exist_ok=True)
        for dep in dependencies["libraries"]:
            if not dep["platform"]:
                native.checked_copy(dep["path"], runtime / "lib" / dep["soname"], limits)
        package = source_package(src, m, tmp / "source.zip", limits)
        r = {"store_registration_version": 1, "workload": "mutable_store", "protocol_version": 1,
             "build_cache_key": build_key, "source_digest": digest(source_files), "source_files": source_files,
             "source_package": package, "build_files": build_files, "dependencies": dependencies,
             "program_paths": sorted(program_names),
             "runtime_files": native.scan(runtime, [p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file()], limits)}
        r["candidate_digest"] = digest(r)
        save(tmp / "registration.json", r, 0o444)
        save(tmp / "build-log.json", logs, 0o444)
        for p in tmp.rglob("*"):
            native.check_limits(limits)
            if p.is_file() and not p.stat().st_mode & 0o111:
                p.chmod(0o444)
        dest = parent / r["candidate_digest"]
        native.check_limits(limits)
        if dest.exists():
            verify(dest, limits)
        else:
            os.rename(tmp, dest)
        return r


def verify(root: Path, limits=None) -> tuple[dict, dict]:
    native.check_limits(limits)
    root = Path(root)
    r = load(safe(root, "registration.json"), 32 * 1024 * 1024)
    if (r.get("store_registration_version") != 1 or r.get("workload") != "mutable_store"
            or digest({k: v for k, v in r.items() if k != "candidate_digest"}) != r.get("candidate_digest")
            or root.name != r["candidate_digest"]):
        raise Error("stale_mutable_candidate_digest")
    m = validate(load(safe(root / "source", MANIFEST), 16 * 1024 * 1024))
    if native.scan(root / "source", [MANIFEST, *m["source_paths"], *m["artifact_paths"], *m["runtime_paths"]], limits) != r["source_files"]:
        raise Error("stale_mutable_source_digest")
    if digest(r["source_files"]) != r["source_digest"]:
        raise Error("stale_mutable_source_digest")
    if native.scan(root / "runtime", [f["path"] for f in r["runtime_files"]], limits) != r["runtime_files"]:
        raise Error("stale_mutable_runtime_digest")
    if native.checked_sha(root / "source.zip", limits) != r["source_package"]["sha256"]:
        raise Error("stale_mutable_source_package")
    with tempfile.TemporaryDirectory(prefix="clab-store-verify-") as td:
        if source_package(root / "source", m, Path(td) / "source.zip", limits) != r["source_package"]:
            raise Error("noncanonical_mutable_source_accounting")
    if m["language"] == "python":
        for f in m["python_runtime"]["files"]:
            p = safe(root / "runtime", f["path"])
            if native.checked_sha(p, limits) != f["sha256"] or p.stat().st_size != f["bytes"]:
                raise Error("stale_python_runtime_lock")
        for name in m["source_paths"]:
            if native.checked_sha(root / "source" / name, limits) != native.checked_sha(root / "runtime" / name, limits):
                raise Error("mutable_program_source_mismatch")
    else:
        nm = native_manifest(m)
        native.check_tools(nm, limits)
        if native.scan_selected(root / "runtime", nm["build_output_paths"], limits) != r["build_files"]:
            raise Error("stale_mutable_binary_digest")
        if native.closure(root / "runtime" / nm["executable"], nm, limits=limits) != r["dependencies"]:
            raise Error("stale_mutable_dependencies")
    native.runtime_mounts(r["dependencies"], limits=limits)
    return m, r


def costs(m: dict, r: dict) -> dict:
    source = {f["path"]: f for f in r["source_files"]}
    runtime = {f["path"]: f for f in r["runtime_files"]}
    fixed = set(m["artifact_paths"] + m["runtime_paths"])
    language = sum(f["bytes"] for p, f in runtime.items() if p.startswith("python/"))
    program = sum(runtime[p]["bytes"] for p in r["program_paths"])
    dep = sum(f["bytes"] for p, f in runtime.items() if p.startswith("lib/"))
    fixed_bytes = sum(source[p]["bytes"] for p in fixed)
    return {"packed_source_bytes": r["source_package"]["bytes"],
            "raw_source_config_bytes": sum(f["bytes"] for p, f in source.items() if p not in fixed),
            "config_bytes": source[MANIFEST]["bytes"], "fixed_bytes": fixed_bytes,
            "binary_bytes": sum(f["bytes"] for f in r["build_files"]), "program_runtime_bytes": program,
            "language_runtime_bytes": language, "nonplatform_dependency_bytes": dep,
            "platform_dependency_bytes_reported_not_charged": sum(d["bytes"] for d in r["dependencies"]["libraries"] if d["platform"]),
            "deployment_fixed_bytes": fixed_bytes + program + language + dep + source[MANIFEST]["bytes"],
            "source_primary_fixed_bytes": fixed_bytes + r["source_package"]["bytes"] + language + dep,
            "runtime_inventory_bytes": sum(f["bytes"] for f in runtime.values()),
            "dependency_inventory": r["dependencies"],
            "installed_runtime_policy": "Python interpreter, stdlib, dynamic extensions and nonplatform libraries are copied, pinned and charged. Only the 0.1.0 C/C++ platform allowlist is assumed installed."}


def command(m: dict) -> list[str]:
    return [s.replace("{runtime}", "/candidate").replace("{python}", "/candidate/" + m.get("python_runtime", {}).get("executable", "UNSUPPORTED")) for s in m["command"]]


def create_python_candidate(path: Path, source: str, name="tiny-durable-reference") -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    (path / "program.py").write_text(source)
    m = {"schema_version": 1, "workload": "mutable_store", "protocol_version": 1,
         "candidate_id": name, "language": "python", "threads": 1, "deterministic": True,
         "source_paths": ["program.py"], "artifact_paths": [], "runtime_paths": [],
         "command": ["{python}", "-I", "-S", "-B", "{runtime}/program.py"],
         "durability_claim": "process_crash_only", "python_runtime": python_runtime_spec()}
    save(path / MANIFEST, m)
    return path


BROKEN_CONTROLS = {
    "counter_reset": ("        # MUTATION_COUNTER", "        self.head = 0  # deliberately broken counter reset"),
    "stale_generation_collision": ("        # MUTATION_ORPHANS", "        return  # deliberately retain orphan generations"),
    "partial_wal_continuation": ("                store.poisoned = True  # MUTATION_IO_POISON", "                store.poisoned = False  # deliberately continue after partial I/O"),
    "duplicate_export": ("        # MUTATION_EXPORT", "        if name == 'titles': data = data + b'\\n' + data  # deliberately duplicate exported rows"),
    "partial_transaction": ("        table_data = dict(self.tables)  # MUTATION_ATOMIC_STAGE", "        table_data = self.tables  # deliberately mutate before validating all rows"),
    "wrong_join": ("                    if relation['values'][1] == args['id']:  # MUTATION_JOIN", "                    if relation['values'][2] == args['id']:  # deliberately wrong join key"),
}


def create_reference(path: Path, broken: str | None = None) -> Path:
    source = Path(__file__).with_name("reference_store.py").read_text()
    if broken is not None:
        if broken not in BROKEN_CONTROLS:
            raise Error("unknown_broken_store_control")
        old, new = BROKEN_CONTROLS[broken]
        if source.count(old) != 1:
            raise Error("broken_control_patch_drift")
        source = source.replace(old, new)
    name = "tiny-durable-reference" if broken is None else "broken-" + broken.replace("_", "-")
    return create_python_candidate(path, source, name)
