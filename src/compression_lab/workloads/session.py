"""One bounded persistent candidate subprocess in the existing Linux sandbox.

The protocol is sequential, single-writer and stdio only. The evaluator's ledger,
reference state and result directories are never mounted into the candidate.
"""
from __future__ import annotations

import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import candidate as native, runner
from ..util import Error, save
from . import metrics, protocol, registry


class Session:
    def __init__(self, candidate: Path, store: Path, *, timeout=20., memory=1024**3,
                 output_limit=128 * 1024 * 1024, cancel=None, deadline=None,
                 phase="correctness", cache="fresh_process_no_os_cache_drop",
                 runtime_override=None, diagnostic=False):
        if sys.platform != "linux" or not hasattr(os, "sched_getaffinity"):
            raise Error("mutable_isolation_unavailable")
        self.root, self.store = Path(candidate).absolute(), Path(store).absolute()
        self.manifest, self.registration = registry.verify(self.root)
        self.timeout, self.memory, self.output_limit = float(timeout), memory, output_limit
        self.cancel = Path(cancel) if cancel else None
        self.deadline = deadline if deadline is not None else time.monotonic() + 3600
        self.phase, self.cache = phase, cache
        self.operations: list[dict] = []
        self._seq, self._pending = 0, None
        self._buffer, self._stderr = bytearray(), bytearray()
        self._closed, self._failure = False, None
        self._monitor_error = None
        self._returncode, self._ru_peak, self._first_reply = None, 0, None
        self.peak_rss = self.peak_space = self.peak_allocated = self.peak_processes = 0
        self.io_peak: dict[str, int] = {}
        self._monitor_stop = threading.Event()
        self._tmp = tempfile.TemporaryDirectory(prefix="clab-persistent-")
        tmp = Path(self._tmp.name)
        self.store.mkdir(parents=True, exist_ok=True)
        metrics.space(self.store)
        unshare = shutil.which("unshare", path="/usr/bin:/bin")
        if not unshare:
            self._tmp.cleanup()
            raise Error("mutable_isolation_unavailable", "util-linux unshare required")
        runtime = self.root / "runtime" if runtime_override is None else Path(runtime_override[0])
        dependencies = self.registration["dependencies"] if runtime_override is None else runtime_override[1]
        (tmp / "readonly-tmp").mkdir()
        config = {"argv": registry.command(self.manifest), "root": str(tmp / "root"),
                  "readonly": {"/candidate": str(runtime), "/tmp": str(tmp / "readonly-tmp")},
                  "runtime_files": native.runtime_mounts(dependencies, diagnostic),
                  "output": str(self.store), "memory": memory, "output_limit": output_limit,
                  "timeout": min(3600, max(1, self.deadline - time.monotonic())),
                  "build": False, "sanitizer": diagnostic, "cpu": min(os.sched_getaffinity(0))}
        save(tmp / "config.json", config)
        package = Path(runner.__file__).parent
        cmd = [sys.executable, "-I", "-S", str(package / "_launch.py"), str(os.getpid()),
               unshare, "--user", "--map-root-user", "--mount", "--net", "--pid", "--ipc", "--uts",
               "--fork", "--kill-child=KILL", sys.executable, "-I", "-S", str(Path(__file__).with_name("_persistent_sandbox.py")),
               str(tmp / "config.json")]
        self.started_ns = time.perf_counter_ns()
        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            cwd=self.store, start_new_session=True, close_fds=True, bufsize=0,
                                            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C"})
        except BaseException:
            self._tmp.cleanup()
            raise
        self.pid = self.process.pid
        self.selector = selectors.DefaultSelector()
        for pipe, kind in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, kind)
        os.set_blocking(self.process.stdin.fileno(), False)
        self._monitor = threading.Thread(target=self._observe, name="clab-store-observer", daemon=True)
        self._monitor.start()

    def _io(self) -> dict:
        result = {}
        for pid in runner.process_tree(self.pid):
            try:
                for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                    k, v = line.split(":", 1)
                    result[k] = result.get(k, 0) + int(v)
            except (OSError, ValueError):
                pass
        return result

    def _unlinked_space(self):
        """Directory scans omit blocks held by deleted, still-open store files."""
        device = self.store.stat().st_dev
        files = {}
        for pid in runner.process_tree(self.pid):
            try:
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    try:
                        st = fd.stat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(st.st_mode) and st.st_nlink == 0 and st.st_dev == device:
                        files[st.st_dev, st.st_ino] = st
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                # Linux can revoke /proc/fd access during exit before the task
                # disappears. PF_EXITING (sched.h: 0x4) means it cannot resume
                # candidate work; an active process hiding its FDs still fails.
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(')', 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if fields[0] in ('Z', 'X') or int(fields[6]) & 4:
                    continue
                raise Error("mutable_fd_inventory_unavailable", f"Active process {pid}: {exc}") from exc
        return sum(st.st_size for st in files.values()), sum(st.st_blocks * 512 for st in files.values())

    def _observe(self):
        while not self._monitor_stop.is_set():
            try:
                rss, count = runner.usage(self.pid)
                s = metrics.space(self.store)
                unlinked_logical, unlinked_allocated = self._unlinked_space()
                logical = s["logical_bytes"] + unlinked_logical
                allocated = s["allocated_bytes"] + unlinked_allocated
                io = self._io()
                self.peak_rss = max(self.peak_rss, rss)
                self.peak_processes = max(self.peak_processes, count)
                self.peak_space = max(self.peak_space, logical)
                self.peak_allocated = max(self.peak_allocated, allocated)
                for k, v in io.items():
                    self.io_peak[k] = max(self.io_peak.get(k, 0), v)
                if rss > self.memory:
                    self._failure = "mutable_memory_limit"
                elif max(logical, allocated) > self.output_limit:
                    self._failure = "mutable_store_space_limit"
                elif count > 3:
                    self._failure = "mutable_process_limit"
                if self._failure:
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
            except BaseException as exc:
                self._failure = getattr(exc, "code", "mutable_monitor_failed")
                self._monitor_error = f"{type(exc).__name__}: {exc}"
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            self._monitor_stop.wait(.01)

    def _check(self, operation_deadline):
        if self._failure:
            raise Error(self._failure)
        if self.cancel and self.cancel.exists():
            raise Error("cancelled")
        if time.monotonic() > self.deadline:
            raise Error("wall_budget_exhausted")
        if time.monotonic() > operation_deadline:
            raise Error("mutable_operation_timeout")

    def _read_ready(self, wait):
        eof = False
        for key, _ in self.selector.select(wait):
            try:
                b = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not b:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout":
                    eof = True
                continue
            if key.data == "stdout":
                self._buffer.extend(b)
                if len(self._buffer) > protocol.MAX_RESPONSE_BYTES:
                    raise Error("mutable_protocol_response_limit")
            else:
                self._stderr.extend(b)
                if len(self._stderr) > 32768:
                    raise Error("mutable_stderr_limit")
                if b"AddressSanitizer" in self._stderr or b"runtime error:" in self._stderr:
                    raise Error("mutable_sanitizer_failure")
        return eof

    def begin(self, operation: str, args: dict) -> int:
        if self._closed or self._returncode is not None:
            raise Error("mutable_session_closed")
        if self._pending is not None:
            raise Error("mutable_concurrent_request_unsupported")
        self._seq += 1
        raw = protocol.request_bytes(self._seq, operation, args)
        end = min(self.deadline, time.monotonic() + self.timeout)
        before_io = self._io()
        self._pending = {"request_id": self._seq, "op": operation, "phase": self.phase, "cache": self.cache,
                         "start_ns": time.perf_counter_ns(), "deadline": end,
                         "request_bytes": len(raw), "io_before": before_io}
        try:
            self._read_ready(0)
            if self._buffer:
                raise Error("mutable_protocol_unsolicited_output")
            sent = 0
            while sent < len(raw):
                self._check(end)
                try:
                    n = os.write(self.process.stdin.fileno(), raw[sent:])
                    if n <= 0:
                        raise Error("mutable_stdin_closed")
                    sent += n
                except BlockingIOError:
                    self._read_ready(.005)
                except BrokenPipeError as exc:
                    raise Error("mutable_process_exited", self._stderr.decode("utf-8", "replace")) from exc
            return self._seq
        except BaseException as exc:
            self.kill(getattr(exc, "code", "mutable_request_failed"))
            raise

    def finish(self) -> dict:
        if self._pending is None:
            raise Error("mutable_no_pending_request")
        pending = self._pending
        try:
            while b"\n" not in self._buffer:
                self._check(pending["deadline"])
                eof = self._read_ready(.005)
                if eof and b"\n" not in self._buffer:
                    self._check(pending["deadline"])
                    raise Error("mutable_process_exited", self._stderr.decode("utf-8", "replace"))
            newline = self._buffer.index(10) + 1
            line = bytes(self._buffer[:newline])
            del self._buffer[:newline]
            if self._buffer:
                raise Error("mutable_protocol_extra_output")
            r = protocol.response(line, pending["request_id"])
            end = time.perf_counter_ns()
            self._first_reply = self._first_reply or end
            io_after = self._io()
            row = {k: v for k, v in pending.items() if k not in ("deadline", "io_before", "start_ns")}
            row.update(latency_ns=end - pending["start_ns"], ok=r["ok"], response_bytes=len(line),
                       error_code=None if r["ok"] else r["error"]["code"],
                       io_delta={k: max(0, v - pending["io_before"].get(k, 0)) for k, v in io_after.items()},
                       space_after=metrics.space(self.store), observed_session_peak_rss_bytes=self.peak_rss)
            self.operations.append(row)
            self._pending = None
            return r
        except BaseException as exc:
            self.kill(getattr(exc, "code", "mutable_response_failed"))
            raise

    def request(self, operation: str, args: dict) -> dict:
        self.begin(operation, args)
        return self.finish()

    def _wait(self, seconds=5.):
        end = time.monotonic() + seconds
        while self._returncode is None:
            try:
                pid, status, usage = os.wait4(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._returncode = self.process.returncode
                break
            if pid:
                self._returncode = os.waitstatus_to_exitcode(status)
                self.process.returncode = self._returncode
                self._ru_peak = int(usage.ru_maxrss) * 1024
                break
            if time.monotonic() > end:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(.005)

    def _cleanup(self):
        self._monitor_stop.set()
        self._monitor.join(timeout=2)
        self.selector.close()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
        self._tmp.cleanup()
        self._closed = True

    def kill(self, reason="external_SIGKILL"):
        if self._closed:
            return
        if self._pending:
            pending = self._pending
            row = {k: v for k, v in pending.items() if k not in ("deadline", "io_before", "start_ns")}
            row.update(latency_ns=time.perf_counter_ns() - pending["start_ns"], ok=False,
                       error_code=reason, killed=True, response_bytes=None)
            try:
                row["space_after"] = metrics.space(self.store)
            except BaseException as exc:
                # Candidate-controlled files must never stop process teardown.
                row["space_inventory_error"] = getattr(exc, "code", "mutable_inventory_failed")
            self.operations.append(row)
            self._pending = None
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._wait()
        self._cleanup()

    def close(self):
        if self._closed:
            return
        try:
            r = self.request("close", {})
            if not r["ok"]:
                raise Error("mutable_close_rejected", r["error"]["code"])
            self.process.stdin.close()
            self._wait()
            self._read_ready(0)
            if self._buffer:
                raise Error("mutable_protocol_extra_output")
            if self._returncode != 0:
                self._check(self.deadline)
                raise Error("mutable_close_process_failed", str({"returncode": self._returncode,
                            "monitor_error": self._monitor_error, "stderr": self._stderr.decode("utf-8", "replace")}))
            self._cleanup()
            return r
        except BaseException:
            self.kill("mutable_close_failed")
            raise

    def summary(self) -> dict:
        return {"pid": self.pid, "returncode": self._returncode, "phase": self.phase,
                "spawn_to_first_reply_ns": None if self._first_reply is None else self._first_reply - self.started_ns,
                "cpu": min(os.sched_getaffinity(0)), "sandbox": "required namespaces/chroot/seccomp/read-only root, writable /output only",
                "persistent_single_worker": True, "observed_peak_rss_bytes": self.peak_rss,
                "wait4_peak_rss_bytes": self._ru_peak, "observed_peak_store_bytes": self.peak_space,
                "observed_peak_allocated_bytes": self.peak_allocated, "max_observed_processes": self.peak_processes,
                "io_counters_peak": self.io_peak, "stderr": self._stderr.decode("utf-8", "replace"),
                "monitor_failure": self._failure, "monitor_error": self._monitor_error,
                "observation_interval_seconds": .01,
                "measurement_limits": "RSS/space/I/O polling can miss transients; space includes unlinked open regular store files. Peaks are observed lower bounds, not a kernel aggregate disk quota. RLIMIT_FSIZE and NOFILE additionally bound each file and descriptor count. wait4 also includes namespace/interpreter bootstrap. No OS cache drop."}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.kill("exception_cleanup")
        else:
            self.close()
