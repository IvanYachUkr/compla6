"""Static relational gates reuse native exact-byte, sanitizer and timing gates."""
from pathlib import Path

from ..gates import Evaluation as NativeEvaluation
from ..util import save, sha
from .relational import profile


def evaluate(root, rows, card, out, depth="full", mode="required", cancel=None, deadline=None):
    profiles = [{"alias": row["alias"], **profile(Path(row["source"]).read_bytes())} for row in rows]
    raw = NativeEvaluation(root, rows, card, out, depth, mode, cancel, deadline).run()
    raw["workload"] = "static_relational"
    raw["relational"] = {"format": "RLB1", "objects": profiles,
                         "equality": "Entire bundle, schema spelling, framing, table/row order and all lexical table bytes",
                         "encoding_floor_bytes_per_second": 100_000_000, "decoder_speed_gated": False}
    p = Path(out) / "relational-profile.json"
    save(p, raw["relational"], 0o444)
    raw["evidence_files"] = {p.name: {"bytes": p.stat().st_size, "sha256": sha(p)}}
    save(Path(out) / "raw.json", raw)
    return raw
