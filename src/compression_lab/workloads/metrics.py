"""Operation distributions and complete observed filesystem accounting."""
from __future__ import annotations

import math
import os
import statistics
import stat
from pathlib import Path

from ..util import Error, sha

CATEGORIES = ("wal", "index", "ack", "snapshot", "metadata", "temporary", "other")


def category(name: str) -> str:
    leaf = Path(name).name
    if leaf.startswith(".") or leaf.endswith(".tmp") or leaf in ("fault.ready", "fault.release"):
        return "temporary"
    if leaf.startswith(("wal-", "wal.")) or leaf.endswith(".wal"):
        return "wal"
    if leaf.startswith(("index", "idx")) or leaf.endswith(".idx"):
        return "index"
    if leaf.startswith(("ack", "receipt")):
        return "ack"
    if leaf.startswith(("data-", "snap-", "snapshot")):
        return "snapshot"
    if leaf in ("CURRENT", "LOCK") or leaf.startswith(("meta", "manifest")):
        return "metadata"
    return "other"


def space(root: Path, hashes=False) -> dict:
    root = Path(root)
    files, logical, allocated = [], 0, 0
    totals = {k: 0 for k in CATEGORIES}
    if root.is_symlink():
        raise Error("mutable_store_symlink")
    for base, dirs, leaves in os.walk(root, followlinks=False):
        for d in dirs:
            if (Path(base) / d).is_symlink():
                raise Error("mutable_store_symlink")
        for leaf in leaves:
            p = Path(base) / leaf
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise Error("mutable_store_nonregular_file", p.name)
            name = p.relative_to(root).as_posix()
            logical += st.st_size
            allocated += st.st_blocks * 512
            kind = category(name)
            totals[kind] += st.st_size
            row = {"path": name, "bytes": st.st_size, "allocated_bytes": st.st_blocks * 512, "category": kind}
            if hashes:
                row["sha256"] = sha(p)
            files.append(row)
            if len(files) > 100_000:
                raise Error("mutable_store_file_limit")
    return {"logical_bytes": logical, "allocated_bytes": allocated, "file_count": len(files),
            "categories": totals, "files": sorted(files, key=lambda r: r["path"])}


def distribution(values: list[int]) -> dict:
    if not values:
        return {"count": 0, "samples_ns": []}
    ordered = sorted(values)
    percentile = lambda p: ordered[max(0, math.ceil(p * len(ordered)) - 1)]
    med = statistics.median(values)
    return {"count": len(values), "samples_ns": values, "minimum_ns": min(values),
            "median_ns": med, "mean_ns": statistics.mean(values), "p90_ns": percentile(.90),
            "p95_ns": percentile(.95), "p99_ns": percentile(.99), "maximum_ns": max(values),
            "relative_MAD": statistics.median(abs(x - med) for x in values) / med if med else 0,
            "quantile_rule": "nearest rank, all samples retained; no trimming"}


def operation_distributions(operations: list[dict]) -> dict:
    groups = {}
    for op in operations:
        # Fault and correctness operations remain in raw data, separate from
        # clean metric trials. No cherry-picking a successful trial out of a run.
        key = "/".join((op.get("phase", "correctness"), op.get("cache", "unspecified"), op["op"],
                        "ok" if op.get("ok") else "error-or-killed",
                        op.get("object_alias", "unspecified"),
                        "merge-round-" + str(op["merge_round"]) if "merge_round" in op else "all",
                        op.get("measurement_sequence", "unspecified")))
        groups.setdefault(key, []).append(op["latency_ns"])
    return {key: distribution(values) for key, values in sorted(groups.items())}
