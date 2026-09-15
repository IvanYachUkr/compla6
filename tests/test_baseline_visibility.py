"""Owner baseline evidence must not cross the researcher interface in hidden runs."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import dataset
from compression_lab.engine import Engine, ResearcherEngine
from compression_lab.controller import Controller
from compression_lab.util import Error, load, save
from test_controller import protocol
from test_engine import data

REAL_POPEN = subprocess.Popen


def no_worker_spawn(command, *args, **kwargs):
    if any("from compression_lab.worker" in str(part) for part in command):
        raise AssertionError("visibility guard did not block worker execution")
    return REAL_POPEN(command, *args, **kwargs)


class BaselineVisibilityCardTests(unittest.TestCase):
    def test_hidden_policy_is_an_explicit_sealed_card_choice(self):
        card = dataset.example_card("visibility")
        before = copy.deepcopy(card)
        self.assertEqual(dataset.validate(card), before)
        for choice in ("visible", "hidden"):
            with self.subTest(choice=choice):
                selected = {**card, "baseline_visibility": choice}
                self.assertEqual(dataset.validate(selected)["baseline_visibility"], choice)

    def test_invalid_visibility_fails_at_card_validation(self):
        for choice in (None, False, "blind", [], {}):
            with self.subTest(choice=choice), self.assertRaisesRegex(Error, "invalid_baseline_visibility"):
                dataset.validate({**dataset.example_card("visibility"), "baseline_visibility": choice})


class VisibilityFixture(unittest.TestCase):
    visibility = "hidden"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        card_path = data(root / "data")
        if self.visibility is not None:
            save(card_path, {**load(card_path), "baseline_visibility": self.visibility})
        self.owner = Engine.init(root / "workspace", card_path, exploratory=True)
        self.public = ResearcherEngine(self.owner.root)
        # Build/process execution is deliberately outside this unit/transport test.
        # Registration, reservation, ledger commits, policy, and raw reads are real.
        for target, value in (
            ("compression_lab.engine.runner.HOST_LOCK", root / "host.lock"),
            ("compression_lab.engine.bridge.verify_candidate", None),
            ("compression_lab.engine.runner.require_fingerprint", None),
        ):
            kwargs = ({"side_effect": lambda p: ({"candidate_id": "stored", "threads": 1, "input_domain": "opaque_bytes"}, {})}
                      if target.endswith("verify_candidate") else
                      {"side_effect": lambda p, **kw: p} if target.endswith("require_fingerprint") else {"new": value})
            patched = patch(target, **kwargs)
            patched.start()
            self.addCleanup(patched.stop)
        self.first = self.commit("a" * 64, "agent", 700)
        self.second = self.commit("b" * 64, "agent", 600)
        self.control = self.commit("c" * 64, "baseline", 123)
        self.control_job = self.owner.raw(self.control)["job_id"]
        patched = patch("compression_lab.research.inventory.inspect", return_value={
            "families": {"stored": {"available": True, "installed_version": "fixture", "static_codec_available": True}},
            "recipes": {"stored": {}}})
        patched.start()
        self.addCleanup(patched.stop)
        # A missing visibility guard must fail immediately, never run a codec or
        # spawn a worker while another host campaign owns the benchmark machine.
        for target, effect in (("compression_lab.engine.subprocess.Popen", no_worker_spawn),
                               ("compression_lab.engine.baselines.create", AssertionError("visibility guard did not block factory execution"))):
            patched = patch(target, side_effect=effect)
            patched.start()
            self.addCleanup(patched.stop)

    def register_stub(self, cid, track="agent", public=False):
        registration = {"candidate_digest": cid, "source_digest": "source-" + cid,
                        "source_package": {"bytes": 20}, "build_files": [{"bytes": 40}]}
        with patch("compression_lab.engine.bridge.register_candidate", return_value=registration):
            return (self.public if public else self.owner).register(self.owner.root / "workbench" / "source", track)

    def commit(self, cid, track, cost):
        self.register_stub(cid, track)
        with patch("compression_lab.engine.subprocess.Popen"):
            queued = self.owner.evaluate(cid, "full", track)
        job = self.owner._job(queued["job_id"])
        return self.owner._finish_job(job, {
            "status": "eligible", "quality_passed": True, "eligible": True,
            "wall_seconds": 1, "active_wall_seconds": 1, "reason_codes": [],
            "fixed_costs": {"fixed_bytes": 0},
            "accounting": {"development": {"archive_ratio": cost / 1000,
                "actual": {"archive_bytes": cost}, "projection": {"deployment_total_bytes": cost}}},
            "timing": {"encode": {"median_bytes_per_second": 150000000,
                                   "median_decimal_MB_per_second": 150},
                       "decode": {"median_bytes_per_second": 250000000,
                                   "median_decimal_MB_per_second": 250}},
        })


class BaselineVisibilityTests(VisibilityFixture):
    def test_hidden_discovery_and_candidate_feedback_do_not_supply_controls(self):
        owner_before = self.owner.state()
        responses = [self.public.profile(), self.public.brief(), self.public.status(),
                     self.public.result(self.second), self.public.feedback(self.second),
                     self.public.baseline_table(candidate_result=self.second)]
        encoded = json.dumps(responses)
        self.assertNotIn(self.control, encoded)
        self.assertNotIn("c" * 64, encoded)
        self.assertNotIn(self.control_job, encoded)
        self.assertIn(self.second, encoded)
        table = responses[-1]["metrics"]
        self.assertEqual(table["baseline_visibility"], "hidden")
        self.assertEqual(table["rows"], [])
        self.assertFalse(table["supplied_comparison_available"])
        brief = responses[1]["metrics"]
        self.assertEqual(brief["next_action"], "one_focused_experiment")
        self.assertNotIn("qualified_controls", brief)
        self.assertNotIn("baseline_comparison", responses[3]["metrics"])
        self.assertNotIn("best_compatible_control", responses[4]["metrics"]["qualification"])
        self.assertNotIn("baseline_improved", responses[4]["metrics"]["qualification"])
        self.assertNotIn("baseline", brief["consumed_budgets"])
        self.assertEqual(responses[2]["metrics"]["completed_evaluations"], 2)
        self.assertEqual(responses[2]["metrics"]["registered_candidates"], 2)
        self.assertEqual(responses[2]["metrics"]["latest_result_id"], self.second)
        self.assertEqual(self.owner.state(), owner_before)

    def test_baseline_ids_cannot_be_used_as_generic_results_jobs_or_diagnostics(self):
        destination = self.owner.root / "exports" / "must-not-exist.zip"
        operations = {
            "raw": lambda: self.public.raw(self.control),
            "result": lambda: self.public.result(self.control),
            "status-result": lambda: self.public.status(result=self.control),
            "status-job": lambda: self.public.status(job=self.control_job),
            "artifact": lambda: self.public.artifact(self.control),
            "export": lambda: self.public.export(self.control, destination),
            "feedback": lambda: self.public.feedback(self.control),
            "compare": lambda: self.public.compare([self.control, self.second]),
            "diagnostics": lambda: self.public.compare([self.control, self.second], True),
            "cancel": lambda: self.public.cancel(self.control_job),
            "evaluate-id": lambda: self.public.evaluate("c" * 64),
        }
        before = self.owner.state()
        for name, operation in operations.items():
            with self.subTest(operation=name), self.assertRaises(Error):
                operation()
        self.assertFalse(destination.exists())
        self.assertEqual(self.owner.state(), before)

    def test_hidden_baseline_tools_do_not_run_or_return_cached_evidence(self):
        before = self.owner.state()
        for response in (self.public.baseline_trial("stored", "full", True),
                         self.public.prepare_baselines(["stored"])):
            self.assertEqual(response["metrics"]["baseline_visibility"], "hidden")
            self.assertFalse(response["metrics"]["supplied_comparison_available"])
            self.assertNotIn(self.control, json.dumps(response))
        self.assertEqual(self.owner.state(), before)
        with self.assertRaises(Error):
            self.public.register(self.owner.root / "workbench" / "source", "baseline")
        with self.assertRaises(Error):
            self.public.evaluate("b" * 64, track="baseline")
        self.assertEqual(self.owner.state(), before)

    def test_owner_retains_full_comparison_and_researcher_keeps_own_evidence(self):
        original = (self.owner.root / "results" / self.control / "raw.json").read_bytes()
        self.assertEqual(self.owner.raw(self.control)["accounting"]["development"]["projection"]["deployment_total_bytes"], 123)
        self.assertIn(self.control, json.dumps(self.owner.baseline_table()))
        self.assertIn(self.control, json.dumps(self.owner.compare([self.control, self.second])))
        self.assertEqual(len(self.public.compare([self.first, self.second])["metrics"]["rows"]), 2)
        self.assertEqual(json.loads(self.public.artifact(self.second, limit=65536)["metrics"]["text"])["track"], "agent")
        self.assertEqual((self.owner.root / "results" / self.control / "raw.json").read_bytes(), original)
        # The same standard-codec source may be registered as the researcher's
        # own experiment; ownership follows the attempt, not algorithm names.
        self.register_stub("c" * 64, public=True)
        self.assertIn("c" * 64, self.public.state()["registered"])

    def test_hidden_controller_has_no_improvement_gate_or_control_feedback(self):
        with self.assertRaisesRegex(Error, "hidden_baseline_requires_qualification"):
            Controller.configure(self.owner, {**protocol(), "objective": "improvement"})
        Controller.configure(self.owner, protocol())
        responses = [self.public.experiment_brief(), self.public.feedback(self.second),
                     self.public.resume(), self.public.finish("finish-public", "b" * 64, self.second)]
        self.assertNotIn(self.control, json.dumps(responses))
        self.assertTrue(responses[-1]["metrics"]["accepted"])
        self.assertNotIn("baseline_improved", responses[-1]["metrics"])

    def test_corrupt_baseline_payload_cannot_change_researcher_status(self):
        before = self.public.brief()
        path = self.owner.root / "results" / self.control / "raw.json"
        path.chmod(0o600)
        path.write_text("broken owner baseline payload")
        self.assertEqual(self.public.brief(), before)
        self.assertEqual(self.public.status()["metrics"]["completed_evaluations"], 2)

    def test_default_cli_is_public_and_owner_view_requires_an_explicit_host_option(self):
        from compression_lab.cli import dispatch, parser
        arguments = ["status", "--workspace", str(self.owner.root)]
        self.assertNotIn(self.control, json.dumps(dispatch(parser().parse_args(arguments))))
        owner = dispatch(parser().parse_args(["--owner-evidence", *arguments]))
        self.assertIn(self.control, json.dumps(owner))
        with self.assertRaises(Error):
            dispatch(parser().parse_args(["artifact", "--workspace", str(self.owner.root), "--result", self.control]))

    def test_host_controller_completion_cannot_reintroduce_a_supplied_comparison(self):
        controller = Controller.configure(self.owner, protocol())
        controller.stop("operator_stop")
        public = self.public.experiment_brief()
        self.assertNotIn(self.control, json.dumps(public))
        self.assertNotIn("baseline_improved", public["metrics"]["completion"])

    def test_same_source_after_own_registration_gets_a_fresh_agent_job(self):
        old = (self.owner.root / "results" / self.control / "raw.json").read_bytes()
        self.register_stub("c" * 64, public=True)
        with patch("compression_lab.engine.subprocess.Popen"):
            queued = self.public.evaluate("c" * 64, request_id="my-standard-codec")
        self.assertNotEqual(queued["job_id"], self.control_job)
        self.assertEqual(self.owner._job(queued["job_id"])["track"], "agent")
        raw = self.owner.raw(self.control)
        for key in ("run_id", "job_id", "candidate_digest", "card_digest", "runtime_digest", "track", "completed_at", "gate_digest"):
            raw.pop(key, None)
        rid = self.owner._finish_job(self.owner._job(queued["job_id"]), raw)
        self.assertNotEqual(rid, self.control)
        self.assertEqual(self.public.raw(rid)["track"], "agent")
        self.assertEqual(self.public.status(job=queued["job_id"])["metrics"]["result_id"], rid)
        self.assertNotIn(self.control, json.dumps(self.public.result(rid)))
        self.assertEqual((self.owner.root / "results" / self.control / "raw.json").read_bytes(), old)

    def test_lessons_cannot_import_owner_baseline_evidence(self):
        def entry(reference, candidate, name):
            return {"id": name, "kind": "observation", "interpretation": "Measured size changed.",
                    "evidence_result_ids": [reference, candidate],
                    "claim": {"metric": "package_bytes", "relation": "lower",
                              "reference_result_id": reference, "candidate_result_id": candidate}}
        for engine in (self.owner, self.public):
            with self.subTest(engine=type(engine).__name__), self.assertRaises(Error):
                engine.lesson_propose(entry(self.second, self.control, "supplied-control"))
        self.public.lesson_propose(entry(self.first, self.second, "own-observation"))
        listed = self.public.lesson_list()["metrics"]
        self.assertEqual([item["id"] for item in listed["entries"]], ["own-observation"])
        self.assertNotIn(self.control, json.dumps(listed))


class DefaultVisibleCompatibilityTests(VisibilityFixture):
    visibility = None

    def test_missing_policy_keeps_all_existing_baseline_and_result_views(self):
        self.assertEqual(self.public.state(), self.owner.state())
        self.assertEqual(self.public.baseline_table(), self.owner.baseline_table())
        self.assertEqual(self.public.result(self.control), self.owner.result(self.control))
        self.assertEqual(self.public.status(job=self.control_job), self.owner.status(job=self.control_job))
        self.assertEqual(self.public.artifact(self.control), self.owner.artifact(self.control))
        self.assertEqual(self.public.compare([self.control, self.second]), self.owner.compare([self.control, self.second]))
        self.assertTrue(self.public.brief()["metrics"]["qualified_controls"]["best_size"])


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Official MCP SDK unavailable; not a protocol pass")
class BaselineVisibilityMCPTests(VisibilityFixture, unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_blocks_baseline_ids_and_keeps_own_result_artifacts(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        package_root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "compression_lab.mcp_server", "--workspace", str(self.owner.root)],
            env={"PYTHONPATH": str(package_root / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertFalse({"owner_evidence", "raw", "controller_host", "baselines", "baseline_trial", "finish", "experiment_brief"} & names)
                async def call(name, arguments=None):
                    return await session.call_tool(name, arguments or {})
                for name, arguments in (
                    ("brief", {}), ("profile", {}), ("status", {}),
                    ("status", {"result_id": self.second}), ("artifact", {"result_id": self.second})):
                    response = await call(name, arguments)
                    self.assertFalse(response.isError, response)
                    value = response.structuredContent or json.loads(response.content[0].text)
                    self.assertNotIn(self.control, json.dumps(value))
                    self.assertNotIn(self.control_job, json.dumps(value))
                    if name in ("baselines", "baseline_trial"):
                        self.assertFalse(value["metrics"]["supplied_comparison_available"])
                for name, arguments in (
                    ("status", {"result_id": self.control}), ("status", {"job_id": self.control_job}),
                    ("artifact", {"result_id": self.control}), ("export", {"result_id": self.control}),
                    ("feedback", {"result_id": self.control}), ("cancel", {"job_id": self.control_job}),
                    ("compare", {"result_ids": [self.control, self.second], "diagnostics": True}),
                    ("evaluate", {"candidate_digest": "c" * 64})):
                    response = await call(name, arguments)
                    self.assertTrue(response.isError, (name, response))
                    self.assertNotIn(self.control, json.dumps(response.model_dump()))
