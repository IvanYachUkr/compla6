"""Evaluator-side bounded JSONL protocol. Not shared with candidate code."""
from __future__ import annotations

import base64
import json

from ..util import Error, canonical, pairs

VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 5 * 1024 * 1024


def request_bytes(request_id: int, operation: str, args: dict) -> bytes:
    if type(request_id) is not int or not 1 <= request_id < (1 << 63):
        raise Error("mutable_protocol_request_id")
    if not isinstance(operation, str) or not 1 <= len(operation) <= 64 or not isinstance(args, dict):
        raise Error("mutable_protocol_request_shape")
    raw = canonical({"version": VERSION, "id": request_id, "op": operation, "args": args}) + b"\n"
    if len(raw) > MAX_REQUEST_BYTES:
        raise Error("mutable_protocol_request_limit")
    return raw


def response(raw: bytes, request_id: int) -> dict:
    if not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE_BYTES:
        raise Error("mutable_protocol_response_limit")
    try:
        r = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Error("mutable_protocol_invalid_json") from exc
    if not isinstance(r, dict) or type(r.get("version")) is not int or r["version"] != VERSION:
        raise Error("mutable_protocol_version")
    if type(r.get("id")) is not int or r["id"] != request_id:
        raise Error("mutable_protocol_response_id")
    if type(r.get("ok")) is not bool:
        raise Error("mutable_protocol_response_shape")
    payload = "result" if r["ok"] else "error"
    if set(r) != {"version", "id", "ok", payload} or not isinstance(r[payload], dict):
        raise Error("mutable_protocol_response_shape")
    if not r["ok"]:
        error = r["error"]
        if (set(error) - {"code", "message", "recovery_required"}
                or not isinstance(error.get("code"), str) or not 1 <= len(error["code"]) <= 128
                or not isinstance(error.get("message", ""), str) or len(error.get("message", "")) > 2000
                or type(error.get("recovery_required", False)) is not bool):
            raise Error("mutable_protocol_error_shape")
    return r


def decode_blob(value: str, limit: int = MAX_DECODED_BYTES) -> bytes:
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise Error("mutable_protocol_blob_limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise Error("mutable_protocol_invalid_base64") from exc
    if len(data) > limit or base64.b64encode(data).decode("ascii") != value:
        raise Error("mutable_protocol_noncanonical_base64")
    return data
