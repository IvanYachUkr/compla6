"""Scoped lessons must derive support from committed public measurements."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab.refinement import RefinementStore
from compression_lab.util import Error, Ledger, canonical, digest, save


class PublicEngine:
    """Cheap public evidence boundary; no codec runs or private data exist here."""

    def __init__(self, root):
        self.root = root
        self.current = {"run_id": "run-fixture", "card_digest": "a" * 64,
                        "runtime_digest": "b" * 64, "registered": [], "results": []}
        resource = {"id": "primary-one", "threads": 1, "cpus": [0], "memory_bytes": 1048576}
        self.card = {"workload": "independent_objects", "_primary_profile": "primary-one",
                     "_resource_profile": resource, "timing_policy": {"scope": "validation-only-v2"},
                     "accounting_policy": "supervisor-decoder-v1"}
        self.pin = {"resource_profile": resource, "resource_profile_digest": digest(resource),
                    "runtime": {"runtime_digest": "b" * 64}}
        self.rows = [{"alias": "dev", "group": "g", "split": "development", "order": 0,
                      "canonical_bytes": 1000, "canonical_sha256": "c" * 64}]
        self.results = {}

    def state(self):
        return copy.deepcopy(self.current)

    def metadata(self):
        return copy.deepcopy(self.card), copy.deepcopy(self.rows)

    def _profile_context(self, profile_id=None):
        if profile_id not in (None, self.card["_primary_profile"]):
            raise Error("unknown_resource_profile")
        return copy.deepcopy(self.card), copy.deepcopy(self.pin)

    def raw(self, result_id):
        if result_id not in self.current["results"]:
            raise Error("result_not_committed")
        raw = copy.deepcopy(self.results[result_id])
        if any(raw[key] != self.current[key] for key in ("run_id", "card_digest", "runtime_digest")):
            raise Error("result_context_mismatch")
        return raw

    def result(self, name, cost=100, candidate="candidate-a", **changes):
        raw = {"job_id": name, "run_id": self.current["run_id"],
               "card_digest": self.current["card_digest"], "runtime_digest": self.current["runtime_digest"],
               "candidate_digest": candidate, "depth": "full", "status": "eligible", "quality_passed": True,
               "workload": self.card["workload"], "track": "agent", "resource_profile": "primary-one",
               "resource_profile_details": copy.deepcopy(self.pin["resource_profile"]),
               "resource_profile_digest": self.pin["resource_profile_digest"],
               "resource_runtime_digest": self.pin["runtime"]["runtime_digest"],
               "timing_scope": self.card["timing_policy"]["scope"], "accounting_policy": self.card["accounting_policy"],
               "decoder_accounting": {"development": {"actual": {"deployment_total_bytes": cost}}},
               "accounting": {"development": {"projection": {"deployment_total_bytes": 999999}}},
               "timing": {"encode": {"median_bytes_per_second": 2000},
                          "decode": {"median_bytes_per_second": 4000}}, **changes}
        result_id = "r-" + digest(raw)
        self.results[result_id] = raw
        self.current["results"].append(result_id)
        self.current["registered"] = sorted(set(self.current["registered"] + [candidate]))
        return result_id


class RefinementEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = PublicEngine(Path(self.temp.name))
        self.reference = self.engine.result("reference", 100, "candidate-reference")
        self.candidate = self.engine.result("candidate", 80, "candidate-proposed")

    def store(self):
        return RefinementStore(self.engine)

    def entry(self, **changes):
        return {"id": "dictionary-size", "kind": "observation",
                "interpretation": "A smaller dictionary may explain this size reduction.",
                "evidence_result_ids": [self.reference, self.candidate],
                "claim": {"metric": "package_bytes", "relation": "lower",
                          "reference_result_id": self.reference, "candidate_result_id": self.candidate},
                **changes}

    def test_proposal_separates_measured_accounted_cost_from_untrusted_explanation(self):
        store = self.store()
        result = store.propose(self.entry())
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["interpretation_status"], "unverified_agent_interpretation")
        self.assertEqual(result["observed"]["reference_value"], 100)
        self.assertEqual(result["observed"]["candidate_value"], 80)
        self.assertEqual(result["observed"]["difference"], -20)
        self.assertTrue(result["observed"]["supports_claim"])
        self.assertFalse(result["trusted_policy"])
        self.assertEqual(result["evidence_result_ids"], [self.reference, self.candidate])
        self.assertNotIn("expected_outcome", result["observed"])
        reloaded = self.store().propose(self.entry())
        self.assertEqual(reloaded["version"], 1)
        self.assertEqual(reloaded["content_digest"], result["content_digest"])

    def test_agent_metrics_and_other_extra_fields_cannot_enter_the_evidence_contract(self):
        store = self.store()
        invalid = [self.entry(metrics={"package_bytes": 1}), self.entry(kind="policy"),
                   self.entry(interpretation="x" * 4001), self.entry(interpretation=""),
                   self.entry(evidence_result_ids=[self.reference]), self.entry(id="../outside"),
                   self.entry(evidence_result_ids=[self.reference, self.reference]),
                   self.entry(procedure={"candidate_digest": "candidate-proposed", "path": "codec.c"})]
        for key, value in [("metric", "agent_score"), ("relation", "approximately"),
                           ("expected_outcome", 1), ("candidate_result_id", self.reference)]:
            invalid.append(self.entry(claim={**self.entry()["claim"], key: value}))
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(Error):
                store.propose(entry)

    def test_public_commit_quality_and_complete_primary_identity_are_required(self):
        store = self.store()
        uncommitted = "r-" + "f" * 64
        with self.assertRaises(Error) as caught:
            store.propose(self.entry(evidence_result_ids=[self.reference, uncommitted],
                                     claim={**self.entry()["claim"], "candidate_result_id": uncommitted}))
        self.assertEqual(caught.exception.code, "result_not_committed")
        changes = [{"depth": "screen"}, {"quality_passed": False}, {"quality_passed": 1},
                   {"status": "failed"}, {"data_scope": "private"}, {"private_attempt": 1},
                   {"resource_profile": "reference-one"}, {"resource_profile_digest": None},
                   {"resource_profile_details": None}, {"resource_runtime_digest": "changed"},
                   {"timing_scope": "changed"}, {"accounting_policy": "standalone-v1"},
                   {"workload": "static_relational"}]
        for index, change in enumerate(changes):
            candidate = self.engine.result(f"bad-{index}", 80, "candidate-proposed", **change)
            with self.subTest(change=change), self.assertRaises(Error):
                store.propose(self.entry(evidence_result_ids=[self.reference, candidate],
                                         claim={**self.entry()["claim"], "candidate_result_id": candidate}))

    def test_invalid_json_types_raise_core_errors_without_creating_lessons(self):
        store = self.store()
        for entry in (None, [], self.entry(claim={**self.entry()["claim"], "metric": []}),
                      self.entry(evidence_result_ids=[{}, self.candidate]),
                      self.entry(interpretation={"text": "claimed"}), self.entry(id=42)):
            with self.subTest(entry=entry), self.assertRaises(Error):
                store.propose(entry)
        self.assertEqual(store.list()["entries"], [])

    def test_missing_identity_fields_cannot_inherit_comparison_defaults(self):
        store = self.store()
        for field in ("resource_runtime_digest", "resource_profile", "timing_scope", "accounting_policy"):
            with self.subTest(field=field):
                raw = copy.deepcopy(self.engine.results[self.candidate])
                raw.pop(field)
                rid = "r-" + digest(raw)
                self.engine.results[rid] = raw
                self.engine.current["results"].append(rid)
                with self.assertRaises(Error):
                    store.propose(self.entry(evidence_result_ids=[self.reference, rid],
                                             claim={**self.entry()["claim"], "candidate_result_id": rid}))

    def test_failed_claim_is_recorded_without_promoting_its_interpretation(self):
        store = self.store()
        result = store.propose(self.entry(claim={**self.entry()["claim"], "relation": "higher"}))
        self.assertEqual(result["status"], "provisional")
        self.assertFalse(result["observed"]["supports_claim"])
        self.assertFalse(result["trusted_policy"])
        self.assertEqual(store.list()["entries"], [])
        self.assertEqual(store.snapshot()["entry_versions"], {})

    def test_throughput_uses_observed_medians_and_rejects_invalid_numbers(self):
        store = self.store()
        for metric in ("encode_bytes_per_second", "decode_bytes_per_second"):
            result = store.propose(self.entry(id=metric, claim={**self.entry()["claim"], "metric": metric,
                                                              "relation": "higher"}))
            self.assertEqual(result["observed"]["reference_value"], 2000 if metric.startswith("encode") else 4000)
            self.assertFalse(result["observed"]["supports_claim"])
        for value in (True, -1, "fast", None):
            candidate = self.engine.result("bad-speed-" + str(value), candidate="candidate-proposed",
                                           timing={"encode": {"median_bytes_per_second": value}})
            with self.subTest(value=value), self.assertRaises(Error):
                store.propose(self.entry(evidence_result_ids=[self.reference, candidate],
                                         claim={"metric": "encode_bytes_per_second", "relation": "higher",
                                                "reference_result_id": self.reference, "candidate_result_id": candidate}))

    def test_fresh_repeat_can_support_then_refute_only_the_structured_claim(self):
        store = self.store()
        proposed = store.propose(self.entry())
        reference = self.engine.result("repeat-reference", 110, "candidate-reference")
        candidate = self.engine.result("repeat-candidate", 90, "candidate-proposed")
        checked = store.check(proposed["id"], [reference, candidate])
        self.assertEqual(checked["status"], "supported_within_scope")
        self.assertEqual(checked["version"], 2)
        self.assertEqual(checked["observed"]["reference_result_id"], reference)
        self.assertEqual(checked["observed"]["candidate_value"], 90)
        self.assertEqual(checked["interpretation_status"], "unverified_agent_interpretation")
        self.assertFalse(checked["trusted_policy"])
        self.assertEqual(store.list()["entries"][0]["status"], "supported_within_scope")
        reference2 = self.engine.result("refute-reference", 100, "candidate-reference")
        candidate2 = self.engine.result("refute-candidate", 120, "candidate-proposed")
        refuted = store.check(proposed["id"], [reference2, candidate2])
        self.assertEqual(refuted["status"], "refuted")
        self.assertEqual(refuted["version"], 3)
        self.assertEqual(refuted["observed"]["difference"], 20)
        self.assertEqual(store.list()["entries"], [])
        self.assertEqual(store.snapshot()["entry_versions"], {})

    def test_checks_reject_reused_wrong_order_and_different_candidate_evidence(self):
        store = self.store()
        store.propose(self.entry())
        reference = self.engine.result("new-reference", 100, "candidate-reference")
        candidate = self.engine.result("new-candidate", 80, "candidate-proposed")
        unrelated = self.engine.result("unrelated", 50, "unrelated")
        for ids in ([reference], [reference, candidate, unrelated], [self.reference, candidate],
                    [reference, reference], [candidate, reference], [reference, unrelated]):
            with self.subTest(ids=ids), self.assertRaises(Error):
                store.check("dictionary-size", ids)
        store.check("dictionary-size", [reference, candidate])
        with self.assertRaises(Error):
            store.check("dictionary-size", [reference, candidate])

    def test_refutation_cannot_be_erased_by_rechecking_or_rephrasing_the_same_claim(self):
        store = self.store()
        store.propose(self.entry())
        reference = self.engine.result("negative-reference", 100, "candidate-reference")
        candidate = self.engine.result("negative-candidate", 120, "candidate-proposed")
        store.check("dictionary-size", [reference, candidate])
        reference2 = self.engine.result("positive-reference", 100, "candidate-reference")
        candidate2 = self.engine.result("positive-candidate", 80, "candidate-proposed")
        with self.assertRaises(Error): store.check("dictionary-size", [reference2, candidate2])
        with self.assertRaises(Error): store.propose(self.entry(interpretation="The same claim, more confidently."))
        self.assertEqual(store.propose(self.entry())["status"], "refuted")
        self.assertEqual(store.list()["entries"], [])
        self.assertEqual(store.rollback("dictionary-size", 1)["status"], "provisional")

    def test_context_drift_expires_retrieval_and_blocks_rollback_and_check(self):
        for field in ("card", "runtime", "data", "timing", "profile"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                self.engine = PublicEngine(Path(directory))
                self.reference = self.engine.result("reference", 100, "candidate-reference")
                self.candidate = self.engine.result("candidate", 80, "candidate-proposed")
                store = self.store()
                store.propose(self.entry())
                if field == "card": self.engine.current["card_digest"] = "e" * 64
                elif field == "runtime": self.engine.current["runtime_digest"] = "d" * 64
                elif field == "data": self.engine.rows[0]["canonical_sha256"] = "e" * 64
                elif field == "timing": self.engine.card["timing_policy"]["scope"] = "changed"
                else:
                    self.engine.card["_resource_profile"]["cpus"] = [1]
                    self.engine.pin["resource_profile_digest"] = digest(self.engine.pin["resource_profile"])
                self.assertEqual(store.list()["entries"], [])
                self.assertEqual(store.snapshot()["entry_versions"], {})
                with self.assertRaises(Error): store.rollback("dictionary-size", 1)
                with self.assertRaises(Error): store.check("dictionary-size", [self.reference, self.candidate])

    def test_updated_proposals_and_owner_rollback_preserve_immutable_version_history(self):
        store = self.store()
        first = store.propose(self.entry())
        frozen = store.snapshot()
        second = store.propose(self.entry(interpretation="A different possible explanation."))
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["status"], "provisional")
        self.assertNotEqual(store.snapshot()["digest"], frozen["digest"])
        restored = store.rollback("dictionary-size", 1)
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["interpretation"], first["interpretation"])
        self.assertEqual(restored["restored_from_version"], 1)
        self.assertEqual(frozen["entry_versions"], {"dictionary-size": 1})
        self.assertEqual(self.store().snapshot(), store.snapshot())
        restored["interpretation"] = "caller mutation"
        self.assertEqual(store.list()["entries"][0]["interpretation"], first["interpretation"])
        events = [json.loads(line) for line in
                  (self.engine.root / "refinements/dictionary-size/events.jsonl").read_text().splitlines()]
        self.assertEqual([event["state"]["version"] for event in events], [1, 2, 3])
        self.assertEqual(events[1]["state"]["previous_content_digest"], first["content_digest"])
        self.assertEqual(events[2]["state"]["previous_content_digest"], second["content_digest"])
        with self.assertRaises(Error): store.rollback("dictionary-size", 20)

    def test_retrieval_rederives_metrics_instead_of_trusting_a_rehashed_memory_record(self):
        store = self.store()
        store.propose(self.entry())
        ledger = Ledger(self.engine.root / "refinements/dictionary-size")
        def forged_metrics(record):
            record["observed"] = {**record["observed"], "candidate_value": 1, "difference": -99}
            record["content_digest"] = digest({key: value for key, value in record.items() if key != "content_digest"})
            return record
        ledger.update("forged_memory_metrics", forged_metrics)
        self.assertEqual(store.list()["entries"], [])
        self.assertEqual(store.snapshot()["entry_versions"], {})

    def test_retrieval_cannot_promote_stored_prose_to_trusted_policy(self):
        store = self.store()
        store.propose(self.entry())
        ledger = Ledger(self.engine.root / "refinements/dictionary-size")
        def forged_policy(record):
            record["trusted_policy"] = True
            record["interpretation_status"] = "verified_fact"
            record["content_digest"] = digest({key: value for key, value in record.items() if key != "content_digest"})
            return record
        ledger.update("forged_policy", forged_policy)
        self.assertEqual(store.list()["entries"], [])

    def test_list_is_bounded_and_only_contains_public_scope_metadata(self):
        store = self.store()
        for number in range(10): store.propose(self.entry(id=f"lesson-{number}"))
        listed = store.list()
        self.assertEqual(len(listed["entries"]), 8)
        self.assertEqual(len(store.list(limit=2)["entries"]), 2)
        self.assertTrue(all(row["status"] == "provisional" for row in listed["entries"]))
        self.assertTrue(all(row["evidence_result_ids"] == [self.reference, self.candidate]
                            for row in listed["entries"]))
        self.assertNotIn(str(self.engine.root), json.dumps(store.snapshot()))
        for limit in (0, -1, 129, True, 1.5):
            with self.assertRaises(Error): store.list(limit=limit)

    def test_collection_reuses_evidence_only_within_one_read(self):
        store = self.store()
        for number in range(4): store.propose(self.entry(id=f"lesson-{number}"))
        with patch.object(self.engine, "raw", wraps=self.engine.raw) as read:
            self.assertEqual(len(store.list(limit=1)["entries"]), 1)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(len(store.snapshot()["entry_versions"]), 4)
            self.assertEqual(read.call_count, 4)
            self.engine.results[self.candidate]["decoder_accounting"]["development"]["actual"]["deployment_total_bytes"] = 81
            self.assertEqual(store.list()["entries"], [])
            self.assertEqual(read.call_count, 6)

    def test_corrupt_evidence_is_excluded_without_hiding_readable_lessons(self):
        store = self.store()
        store.propose(self.entry())
        damaged = self.engine.result("damaged", 80, "candidate-proposed")
        store.propose(self.entry(id="damaged-lesson", evidence_result_ids=[self.reference, damaged],
            claim={**self.entry()["claim"], "candidate_result_id": damaged}))
        original = self.engine.raw
        for failure in (json.JSONDecodeError("truncated", "{", 1), OSError("unreadable"),
                        TypeError("invalid artifact schema"), [], {"timing": []}):
            def read(result_id):
                if result_id == damaged:
                    if isinstance(failure, Exception): raise failure
                    return failure
                return original(result_id)
            with self.subTest(failure=type(failure).__name__), patch.object(self.engine, "raw", side_effect=read):
                listed = store.list()
                self.assertEqual([entry["id"] for entry in listed["entries"]], ["dictionary-size"])
                self.assertEqual(listed["excluded"]["expired"], 1)
                self.assertIn("damaged-lesson", listed["excluded"]["errors"])
                self.assertEqual(store.snapshot()["entry_versions"], {"dictionary-size": 1})

    def test_malformed_lesson_record_is_reported_without_hiding_other_lessons(self):
        store = self.store()
        store.propose(self.entry())
        store.propose(self.entry(id="damaged-lesson"))
        ledger = Ledger(self.engine.root / "refinements/damaged-lesson")
        def damage(record):
            record.pop("observed")
            return record
        ledger.update("fixture_malformed_record", damage)
        listed = store.list()
        self.assertEqual([entry["id"] for entry in listed["entries"]], ["dictionary-size"])
        self.assertEqual(listed["excluded"]["errors"]["damaged-lesson"], "invalid_refinement_record")

    def procedure(self):
        contents = b"/* A readable source reference; never execute this. */\n"
        manifest = canonical({"source_paths": ["codec.c"]}) + b"\n"
        records = [{"path": name, "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                   for name, value in [("candidate.json", manifest), ("codec.c", contents)]]
        registration = {"source_files": records, "source_digest": digest(records)}
        cid = digest(registration)
        directory = self.engine.root / "candidates" / cid
        (directory / "source").mkdir(parents=True)
        (directory / "source/codec.c").write_bytes(contents)
        (directory / "source/candidate.json").write_bytes(manifest)
        save(directory / "registration.json", {**registration, "candidate_digest": cid})
        self.candidate = self.engine.result("procedure-candidate", 80, cid)
        return self.entry(kind="procedure", procedure={"candidate_digest": cid, "path": "codec.c"}), directory

    def test_procedure_is_only_a_hashed_registered_source_reference(self):
        store = self.store()
        entry, directory = self.procedure()
        result = store.propose(entry)
        self.assertEqual(result["procedure"]["sha256"], hashlib.sha256((directory / "source/codec.c").read_bytes()).hexdigest())
        self.assertEqual(result["procedure"]["path"], "codec.c")
        self.assertEqual(result["procedure"]["use"], "readable_reference_only")
        (directory / "source/codec.c").write_text("changed")
        self.assertEqual(store.list()["entries"], [])
        with self.assertRaises(Error): store.propose(entry)

    def test_procedure_rejects_escapes_symlinks_non_sources_and_unregistered_snapshots(self):
        store = self.store()
        entry, directory = self.procedure()
        for path in ("../outside", "/etc/passwd", "source/codec.c", "candidate.json", "missing.c"):
            with self.subTest(path=path), self.assertRaises(Error):
                store.propose({**entry, "procedure": {**entry["procedure"], "path": path}})
        self.engine.current["registered"].remove(entry["procedure"]["candidate_digest"])
        with self.assertRaises(Error): store.propose(entry)
        self.engine.current["registered"].append(entry["procedure"]["candidate_digest"])
        (directory / "source/codec.c").unlink()
        (directory / "source/codec.c").symlink_to(directory / "source/candidate.json")
        with self.assertRaises(Error): store.propose(entry)

    def test_dangling_journal_symlink_cannot_redirect_ledger_creation(self):
        store = self.store()
        lesson = self.engine.root / "refinements/dictionary-size"
        lesson.mkdir()
        target = self.engine.root / "outside-journal"
        (lesson / "events.jsonl").symlink_to(target)
        with self.assertRaises(Error): store.propose(self.entry())
        self.assertFalse(target.exists())

    def test_replaced_store_directory_cannot_redirect_mutations(self):
        store = self.store()
        root = self.engine.root / "refinements"
        root.rmdir()
        target = self.engine.root / "elsewhere"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Error): store.propose(self.entry())
        self.assertEqual(list(target.iterdir()), [])

    def test_real_engine_public_raw_boundary_rejects_digest_valid_uncommitted_evidence(self):
        from compression_lab.engine import Engine
        from test_engine import data

        self.engine = Engine.init(Path(self.temp.name) / "real-workspace",
                                  data(Path(self.temp.name) / "real-data"), exploratory=True)
        card, pin = self.engine._profile_context()
        def finish(name, cost):
            job = {"job_id": name, "run_id": self.engine.state()["run_id"], "candidate_digest": name,
                   "depth": "full", "status": "running", "track": "agent", "created_epoch": time.time(),
                   "resource_profile": card["_resource_profile"]["id"],
                   "resource_profile_digest": pin["resource_profile_digest"],
                   "resource_profile_details": pin["resource_profile"],
                   "resource_runtime_digest": pin["runtime"]["runtime_digest"],
                   "timing_scope": card.get("timing_policy", {}).get("scope", "train-plus-development-v1"),
                   "accounting_policy": card.get("accounting_policy", "standalone-v1")}
            self.engine.ledger.update("fixture_reserve", lambda state: {**state, "active_job": name,
                "budgets": {"agent": {"evaluations": 1, "wall_seconds": 0}}})
            save(self.engine.root / "jobs" / name / "job.json", job)
            return self.engine._finish_job(dict(job), {"status": "eligible", "quality_passed": True,
                "eligible": True, "accounting": {"development": {"projection": {"deployment_total_bytes": cost}}}})
        self.reference, self.candidate = finish("real-reference", 100), finish("real-candidate", 80)
        store = self.store()
        self.assertTrue(store.propose(self.entry())["observed"]["supports_claim"])
        forged = {**self.engine.raw(self.candidate), "job_id": "not-committed"}
        private_id = "r-" + digest(forged)
        save(self.engine.root / "results" / private_id / "raw.json", forged)
        with self.assertRaises(Error) as caught:
            store.propose(self.entry(id="uncommitted", evidence_result_ids=[self.reference, private_id],
                claim={**self.entry()["claim"], "candidate_result_id": private_id}))
        self.assertEqual(caught.exception.code, "result_not_committed")


if __name__ == "__main__":
    unittest.main()
