"""Workload selection and gates; transaction rates are never a static floor."""
from ..util import Error

WORKLOADS = ("independent_objects", "static_relational", "mutable_store")


def measurement_sequences(card):
    return card.get("mutable_policy", {}).get("measurement_sequences", [
        {"transactions": 2, "merges": 2}, {"transactions": 8, "merges": 2},
        {"transactions": 16, "merges": 4}])


def sequence_name(sequence):
    return f"inserts-{sequence['transactions']}-merges-{sequence['merges']}"


def workload(card: dict) -> str:
    return card.get("workload", "independent_objects")


def validate_selection(card: dict):
    kind = workload(card)
    if kind not in WORKLOADS:
        raise Error("unsupported_workload")
    if kind in ("static_relational", "mutable_store"):
        if card.get("adapter") != "relational_bundle":
            raise Error("relational_adapter_required")
    elif card.get("adapter") == "relational_bundle":
        raise Error("relational_workload_required")
    if kind != "mutable_store":
        if "mutable_policy" in card:
            raise Error("unexpected_mutable_policy")
        return
    policy = card.get("mutable_policy", {})
    required = {"protocol_version", "single_writer", "insert_only", "durability", "cache_procedure", "metric_trials"}
    if (not required <= set(policy) or set(policy) - required - {"measurement_sequences"}
            or type(policy["protocol_version"]) is not int or policy["protocol_version"] != 1
            or policy["single_writer"] is not True or policy["insert_only"] is not True
            or policy["durability"] != "process_crash"):
        raise Error("unsupported_mutable_policy", "Only insert-only, single-writer process-crash validation is supported")
    if policy["cache_procedure"] != "fresh_process_then_warm_no_os_cache_drop":
        raise Error("unsupported_cache_procedure", "Cold OS cache or power-loss claims are not certified")
    if type(policy["metric_trials"]) is not int or not 3 <= policy["metric_trials"] <= 11:
        raise Error("invalid_mutable_trial_count")
    sequences = measurement_sequences(card)
    if not isinstance(sequences, list) or not 1 <= len(sequences) <= 4:
        raise Error("invalid_mutable_measurement_sequences")
    previous = (0, 0)
    for sequence in sequences:
        if (not isinstance(sequence, dict) or set(sequence) != {"transactions", "merges"}
                or any(type(v) is not int for v in sequence.values())
                or not 1 <= sequence["merges"] <= sequence["transactions"] <= 256
                or (sequence["transactions"], sequence["merges"]) <= previous):
            raise Error("invalid_mutable_measurement_sequences")
        previous = sequence["transactions"], sequence["merges"]


def validate_bundle(card: dict, raw: bytes):
    from .relational import unpack
    bundle = unpack(raw)
    if workload(card) == "mutable_store":
        import base64
        from .oracle import require_movie_schema
        from .protocol import request_bytes
        require_movie_schema(bundle.schema)
        # Version 1 import is a single bounded request, not hidden chunking.
        request_bytes(1, "build", {"bundle_b64": base64.b64encode(raw).decode("ascii")})
    return bundle
