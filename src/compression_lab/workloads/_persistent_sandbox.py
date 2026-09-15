"""Mutable-only bootstrap: reuse 0.1.0 helpers, then seal the root read-only.

Launched by the trusted evaluator inside new Linux namespaces. The sole writable
regular-file mount is /output; no candidate module is imported by this helper.
"""
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys


def main():
    helper = Path(__file__).resolve().parent.parent / "_sandbox.py"
    spec = importlib.util.spec_from_file_location("clab_trusted_sandbox", helper)
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    with open(sys.argv[1]) as f:
        config = json.load(f)
    if config["build"] or config["runtime_files"] is None:
        raise RuntimeError("Persistent store requires explicit runtime mounts and no build privileges")
    lib, context = sandbox.filter_setup(False)
    root = config["root"]
    os.makedirs(root)
    sandbox.mount(None, "/", flags=16384 | (1 << 18))
    sandbox.mount("tmpfs", root, "tmpfs", 2 | 4, "size=16777216,mode=755")
    for dst, source in config["runtime_files"].items():
        sandbox.bind(source, root + dst)
    for name in ("tmp", "proc", "sys", "dev", "input", "source", "candidate", "output"):
        os.makedirs(root + "/" + name, exist_ok=True)
    for name in ("null", "zero", "urandom", "random"):
        sandbox.bind("/dev/" + name, root + "/dev/" + name, False)
    for dst, source in config["readonly"].items():
        sandbox.bind(source, root + dst)
    sandbox.bind(config["output"], root + "/output", False)
    # Read-only mount root, non-recursive: /output remains a distinct writable
    # bind mount. No hidden root/tmpfs scratch files can escape disk accounting.
    sandbox.mount(None, root, flags=32 | 1)
    os.chroot(root)
    os.chdir("/output")
    os.sched_setaffinity(0, {config["cpu"]})
    for res, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, config["output_limit"]),
                       (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_CPU, max(1, int(config["timeout"]) + 1)),
                       (resource.RLIMIT_NPROC, 1)):
        resource.setrlimit(res, (value, value))
    if not config["sanitizer"]:
        resource.setrlimit(resource.RLIMIT_AS, (config["memory"], config["memory"]))
    sandbox.drop_caps()
    if lib.seccomp_load(context):
        raise RuntimeError("seccomp_load")
    lib.seccomp_release(context)
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/output", "LANG": "C", "LC_ALL": "C",
           "TZ": "UTC", "SOURCE_DATE_EPOCH": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "RAYON_NUM_THREADS": "1", "LD_LIBRARY_PATH": "/candidate/lib:/lib",
           "ASAN_OPTIONS": "abort_on_error=1:detect_leaks=0:allocator_may_return_null=1:symbolize=0",
           "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1"}
    os.execvpe(config["argv"][0], config["argv"], env)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print("mutable sandbox failed: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
