"""Small explicit integration surface for 0.1.0 CLI/MCP/worker calls.

This module never reserves jobs, edits terminal states, launches model calls,
or changes the original engine's ledger/reconciliation implementation.
"""
from __future__ import annotations

from pathlib import Path

from .. import candidate as native, runner, dataset
from ..util import Error, digest, load, reply, safe, save, sha, secure_load, secure_names
from . import cards, registry

EVIDENCE_NAMES = ("mutable-evidence.zip", "relational-profile.json")


def comparison_identity(raw):
    """Only results made under the same declared evaluation contract compare."""
    return (raw.get("card_digest"), raw.get("runtime_digest"),
            raw.get("workload", "independent_objects"), raw.get("depth"),
            raw.get("resource_profile", "legacy-single"), raw.get("resource_profile_digest"),
            raw.get("resource_runtime_digest", raw.get("runtime_digest")),
            raw.get("timing_scope", "train-plus-development-v1"),
            raw.get("accounting_policy", "standalone-v1"))


def decoder_accounting_metrics(raw):
    """Expose the charged decoder terms without substituting a projected legacy cost."""
    actual = raw.get("decoder_accounting", {}).get(dataset.scored_partition(raw), {}).get("actual", {})
    return {key: actual.get(key) for key in ("archive_bytes", "compiled_decoder_bytes",
            "required_decoder_artifact_bytes", "nonplatform_decoder_dependency_bytes",
            "compressed_plus_decoder_bytes", "deployment_total_bytes")}


def verify_candidate(root):
    root = Path(root)
    return registry.verify(root) if (root / "source" / registry.MANIFEST).exists() else native.verify(root)


def select_workload(card, manifest, requested=None):
    kind = cards.workload(card)
    if requested is not None and requested != kind:
        raise Error("candidate_workload_mismatch", "Requested workload must match the immutable dataset card")
    is_store = manifest.get("workload") == "mutable_store"
    if is_store != (kind == "mutable_store"):
        raise Error("candidate_workload_mismatch", "Store protocol and static byte candidates are not interchangeable")
    if not is_store:
        domain = manifest.get("input_domain")
        allowed = {"opaque_bytes"}
        if kind == "static_relational":
            allowed.add("rlb1-lexical")
        elif card["adapter"] == "hcb1":
            allowed.add("hcb1-arbitrary-content")
        if domain not in allowed:
            raise Error("candidate_workload_mismatch", "Candidate input domain does not cover this workload")
    return kind


def register_candidate(root, path, mode, limits, train_hashes, card, cache_info=None):
    path = Path(path)
    names = secure_names(path)
    if registry.MANIFEST in names:
        manifest = registry.validate(secure_load(path, registry.MANIFEST))
        select_workload(card, manifest)
        return registry.register(root, path, mode, limits, train_hashes)
    manifest = native.manifest(secure_load(path, "candidate.json"))
    select_workload(card, manifest)
    return native.register(root, path, mode, limits, train_hashes, cache_info,
                           evaluation_mode=card.get('evaluation_mode','split'))


def evaluate(root, rows, card, out, depth="full", mode="required", cancel=None, deadline=None, workload=None,
             *, data_scope="public", train_hashes=None):
    manifest, _ = verify_candidate(root)
    kind = select_workload(card, manifest, workload)
    if depth == "screen":
        if kind == "mutable_store" or data_scope != "public":
            raise Error("screen_requires_public_static_workload")
        from ..screening import Evaluation
        return Evaluation(root, rows, card, out, depth, mode, cancel, deadline).run()
    if kind == "mutable_store":
        from .mutable import Evaluation
        return Evaluation(root, rows, card, out, depth, mode, cancel, deadline,
                          data_scope=data_scope, train_hashes=train_hashes).run()
    if kind == "static_relational":
        from .static import evaluate as relational_evaluate
        return relational_evaluate(root, rows, card, out, depth, mode, cancel, deadline)
    from ..gates import Evaluation
    return Evaluation(root, rows, card, out, depth, mode, cancel, deadline).run()


def evidence_files(result_root, raw):
    files = {}
    for name, expected in raw.get("evidence_files", {}).items():
        if name not in EVIDENCE_NAMES:
            raise Error("workload_evidence_not_allowlisted")
        p = safe(result_root, name)
        if p.stat().st_size != expected["bytes"] or sha(p) != expected["sha256"]:
            raise Error("stale_workload_evidence_digest", name)
        files[name] = p
    return files


def result_metrics(raw):
    out = {"workload": raw.get("workload", "independent_objects"),
           "resource_profile": raw.get("resource_profile", "legacy-single"),
           "resource_profile_digest": raw.get("resource_profile_digest"),
           "resource_profile_details": raw.get("resource_profile_details"),
           "resource_runtime_digest": raw.get("resource_runtime_digest", raw.get("runtime_digest")),
           "timing_scope": raw.get("timing_scope", "train-plus-development-v1"),
           "accounting_policy": raw.get("accounting_policy", "standalone-v1"),
           "evaluation_mode": raw.get("evaluation_mode", "split"),
           "scored_partition": dataset.scored_partition(raw)}
    if out["accounting_policy"] == "supervisor-decoder-v1":
        out["decoder_accounting_" + dataset.scored_partition(raw) + "_actual"] = decoder_accounting_metrics(raw)
    out['primary_size_policy'] = raw.get('primary_size_policy','strict-deployment-v1')
    out['timing_operation'] = raw.get('timing_operation','legacy-fit-excluded-v1')
    out['encoding_floor_scope'] = raw.get('encoding_floor_scope','encode')
    out['qualified_encoding'] = raw.get('timing',{}).get(dataset.encoding_timing_key(raw))
    if out['timing_operation'] == 'offline-plus-online-v1':
        timing = raw.get('timing', {})
        out['encoding_stages'] = {
            'offline':timing.get('offline'), 'online':timing.get('encode'),
            'combined':timing.get('combined'), 'encoding_floor_scope':raw.get('encoding_floor_scope'),
            'combined_rule':'sum offline and online within each trial, then aggregate; no reuse amortization'}
    if raw.get('reported_accounting'):
        out['reported_accounting_'+dataset.scored_partition(raw)] = raw['reported_accounting'][dataset.scored_partition(raw)]
    if out["workload"] == "mutable_store":
        full = raw.get("mutable_metrics", {})
        out.update(language_runtime_bytes=raw.get("fixed_costs", {}).get("language_runtime_bytes"),
                   program_runtime_bytes=raw.get("fixed_costs", {}).get("program_runtime_bytes"),
                   deployment_fixed_bytes=raw.get("fixed_costs", {}).get("deployment_fixed_bytes"),
                   process_crash_validated=raw.get("process_crash_validated", False),
                   power_loss_certified=False, encoding_floor_applied=False,
                   observed_peak_rss_bytes=full.get("observed_peak_rss_bytes"),
                   observed_peak_store_bytes=full.get("observed_peak_store_bytes"),
                   metric_trial_count=len(full.get("metric_trials", [])),
                   development_store_costs=full.get("development"),
                   mutable_objects=[{"alias": alias, **{k: obj[k] for k in
                         ("median_store_bytes", "deployment_total_bytes", "source_primary_bytes", "split", "rank_sequence")}}
                         for alias, obj in full.get("objects", {}).items()],
                   operation_latency={key: {k: v[k] for k in ("count", "median_ns", "p95_ns", "p99_ns") if k in v}
                         for key, v in full.get("operation_latency", {}).items() if key.startswith("measurement/")})
    return out


def prepare_baselines(engine, depth="full"):
    """An exact-build cached durable reference baseline, not a settings search."""
    state = engine.state()
    if state["stage"] not in ("public_search", "public_ready"):
        raise Error("terminal_latch")
    runner.require_fingerprint(load(engine.root / "runtime.json"))
    source_sha = sha(Path(registry.__file__).with_name("reference_store.py"))
    runtime = registry.python_runtime_spec()
    name = "mutable-reference-" + digest({"source": source_sha, "runtime": runtime["runtime_digest"]})[:24]
    path = engine.root / "workbench" / name
    if not path.exists():
        registry.create_reference(path)
    registered = engine.register(path, "baseline")
    cid = registered["candidate_digest"]
    cached = []
    for rid in state["results"]:
        raw = engine.raw(rid)
        if (raw.get("track") == "baseline" and raw.get("candidate_digest") == cid and raw.get("depth") == depth
                and raw.get("workload") == "mutable_store" and raw["card_digest"] == state["card_digest"]
                and raw["runtime_digest"] == state["runtime_digest"]):
            evidence_files(engine.root / "results" / rid, raw)
            cached.append(rid)
    if not cached:
        engine.evaluate(cid, depth, "baseline", True, workload="mutable_store")
    answer = baseline_table(engine)
    answer["metrics"]["cache_policy"] = "Exact candidate/build, workload/protocol, card, runtime and depth; failed results retained, never silently replaced"
    answer["metrics"]["cached_result_ids"] = cached
    return answer


def baseline_table(engine):
    rows = []
    state = engine.state()
    for rid in state["results"]:
        raw = engine.raw(rid)
        if raw.get("track") == "baseline" and raw.get("workload") == "mutable_store":
            manifest, reg = verify_candidate(engine.root / "candidates" / raw["candidate_digest"])
            rows.append({"name": manifest["candidate_id"], "result_id": rid, "candidate_digest": raw["candidate_digest"],
                         "build_cache_key": reg["build_cache_key"], "depth": raw["depth"], "status": raw["status"],
                         "quality_passed": raw.get("quality_passed", False), "eligible": raw.get("eligible", False), **result_metrics(raw)})
    valid = [r for r in rows if r["quality_passed"] and r["depth"] == "full"
             and (r.get("development_store_costs") or {}).get("deployment_total_bytes") is not None]
    cost = lambda r: r["development_store_costs"]["deployment_total_bytes"]
    return reply(state["run_id"], metrics={"workload": "mutable_store", "rows": rows,
                 "best_size": min(valid, key=cost, default=None),
                 "best_eligible": min((r for r in valid if r["eligible"]), key=cost, default=None),
                 "runtime_digest": state["runtime_digest"], "rank_policy": "Compare complete measured store costs and latency distributions at the same operation counts; no static-size horizon projection"})


def compare(engine, result_ids, diagnostics=False):
    if (not isinstance(result_ids, list) or not 2 <= len(result_ids) <= 8
            or any(not isinstance(x, str) for x in result_ids) or len(set(result_ids)) != len(result_ids)):
        raise Error("invalid_compare_results", "Use two to eight distinct immutable result IDs")
    raw = [engine.raw(rid) for rid in result_ids]
    signatures = {comparison_identity(r) for r in raw}
    if len(signatures) != 1:
        raise Error("incomparable_results", "Card, workload, depth and runtime must match")
    if not isinstance(diagnostics, bool) or (diagnostics and len(raw) != 2):
        raise Error("invalid_diagnostics_comparison", "Diagnostics need exactly two results: reference, then candidate")
    rows = []
    for rid, r in zip(result_ids, raw):
        evidence_files(engine.root / "results" / rid, r)
        manifest, _ = verify_candidate(engine.root / "candidates" / r["candidate_digest"])
        compact = result_metrics(r)
        row = {"result_id": rid, "candidate": manifest["candidate_id"], "candidate_digest": r["candidate_digest"],
               "status": r["status"], "quality_passed": r.get("quality_passed", False), "eligible": r.get("eligible", False),
               "reason_codes": r.get("reason_codes", []), **compact}
        if compact["workload"] != "mutable_store":
            try:
                deployment_total = rank_cost(r)
            except Error as exc:
                if exc.code != "missing_rank_metric":
                    raise
                deployment_total = None
            row.update(deployment_total_bytes=deployment_total,
                       encoding_MBps=r.get("timing", {}).get(dataset.encoding_timing_key(r), {}).get("median_decimal_MB_per_second"),
                       decoding_MBps=r.get("timing", {}).get("decode", {}).get("median_decimal_MB_per_second"))
        rows.append(row)
    metrics = {"rows": rows, "model_calls": 0,
               "interpretation": "Descriptive comparison of preserved trials, not a statistical significance claim or automatic promotion"}
    if diagnostics:
        from ..accounting import paired_diagnostics
        metrics["paired_diagnostics"] = {"reference_result_id": result_ids[0], "candidate_result_id": result_ids[1],
                                         **paired_diagnostics(raw[0], raw[1])}
    return reply(engine.state()["run_id"], metrics=metrics)


def resume(engine):
    state = engine.state()
    next_action = None
    if state["active_job"]:
        engine.status(job=state["active_job"])
        state = engine.state()
    if state["active_job"]:
        next_action = {"command": "status", "job": state["active_job"]}
    elif state["stage"] in ("public_search", "public_ready"):
        cid, rid = state.get("candidate_digest"), state.get("result_id")
        if not cid:
            next_action = {"command": "register", "reason": "No candidate snapshot has been registered"}
        elif not rid or engine.raw(rid)["candidate_digest"] != cid:
            next_action = {"command": "evaluate", "candidate": cid, "depth": "quick"}
        else:
            r = engine.raw(rid)
            if not r.get("quality_passed"):
                next_action = {"command": "register", "reason": "Repair or replace the candidate; the failed attempt remains charged", "failed_result_id": rid}
            elif r.get("depth") != "full":
                next_action = {"command": "evaluate", "candidate": cid, "depth": "full"}
            else:
                selected = state.get("best_eligible_result") or rid
                next_action = {"command": "export", "result": selected, "eligible": engine.raw(selected).get("eligible", False)}
        budget = state["budgets"].get("agent", {"evaluations": 0, "wall_seconds": 0})
        if next_action and next_action["command"] in ("evaluate", "register") and (
                budget["evaluations"] >= state["search_budget"]["candidate_evaluations"]
                or budget["wall_seconds"] >= state["search_budget"]["wall_seconds"]):
            best = state.get("best_eligible_result")
            next_action = {"command": "export", "result": best, "eligible": True} if best else None
    return reply(state["run_id"], state["stage"], metrics={"next_action": next_action,
                "advisory_only": True, "model_calls": 0, "latest_result_id": state.get("result_id"),
                "best_eligible_result_id": state.get("best_eligible_result"),
                "budget_per_track": state["search_budget"], "budgets": state["budgets"]})


def rank_cost(raw):
    if raw.get("workload") == "mutable_store":
        value = raw.get("mutable_metrics", {}).get("development", {}).get("deployment_total_bytes")
    elif raw.get("primary_size_policy") == "standard-codec-available-v1":
        value = raw.get("reported_accounting", {}).get(dataset.scored_partition(raw), {}).get("actual", {}).get("deployment_total_bytes")
    elif raw.get("accounting_policy") == "supervisor-decoder-v1":
        value = raw.get("decoder_accounting", {}).get(dataset.scored_partition(raw), {}).get("actual", {}).get("deployment_total_bytes")
    else:
        value = (raw.get("accounting", {}).get(dataset.scored_partition(raw), {}).get("projection") or {}).get("deployment_total_bytes")
    if value is None:
        raise Error("missing_rank_metric")
    return value
