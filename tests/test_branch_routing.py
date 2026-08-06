from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover - Python 3.6 always has unittest.mock
    import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camflow_pkg import engine
from camflow_pkg.contracts import validate_workflow


def _node(node_id, needs=None, when=None, output_schema=None):
    node = {
        "id": node_id,
        "goal": node_id,
        "steps": ["run " + node_id],
        "needs": list(needs or []),
        "run": {"skill": "local"},
        "output_schema": dict(output_schema or {}),
    }
    if when is not None:
        node["when"] = dict(when)
    return node


def _workflow():
    return {
        "workflow": "test_or_dut_route",
        "version": "1.2",
        "nodes": [
            _node("test_or_dut", output_schema={"route": "string", "evidence": "string"}),
            _node(
                "lsu_debug",
                needs=["test_or_dut"],
                when={"node": "test_or_dut", "path": "data.route", "equals": "lsu_debug"},
                output_schema={"component": "string"},
            ),
            _node(
                "ifu_debug",
                needs=["test_or_dut"],
                when={"node": "test_or_dut", "path": "data.route", "equals": "ifu_debug"},
                output_schema={"component": "string"},
            ),
            _node(
                "summarize",
                needs=["lsu_debug", "ifu_debug"],
                output_schema={"selected": "string"},
            ),
        ],
    }


class BranchRoutingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="camflow-branch-")
        skill = os.path.join(self.directory, "skills", "local")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as handle:
            handle.write("# local\n")

    def tearDown(self):
        shutil.rmtree(self.directory)

    def _run(self, route, name):
        calls = []
        run_dir = os.path.join(self.directory, name)

        def invoke(_root, attempt_dir, node, _prompt, flow=None):
            node_id = node["id"]
            calls.append(node_id)
            if node_id == "test_or_dut":
                data = {"route": route, "evidence": "debug classification"}
            elif node_id in ("lsu_debug", "ifu_debug"):
                data = {"component": node_id}
            else:
                with open(os.path.join(attempt_dir, "input.json"), "r") as handle:
                    attempt_input = json.load(handle)
                selected = sorted((attempt_input.get("upstream") or {}).keys())
                data = {"selected": ",".join(selected)}
            return {
                "status": "success",
                "data": data,
                "error": None,
                "feedback": None,
                "request_human": False,
            }

        with mock.patch.object(engine, "_invoke", side_effect=invoke):
            with mock.patch.object(engine, "_verify", return_value=(True, "verified")):
                result = engine.execute(_workflow(), self.directory, run_dir, None)
        return result, calls, run_dir

    def test_lsu_and_ifu_routes_execute_only_the_selected_branch(self):
        for route, skipped_node in (("lsu_debug", "ifu_debug"), ("ifu_debug", "lsu_debug")):
            with self.subTest(route=route):
                result, calls, run_dir = self._run(route, "run-" + route)
                self.assertEqual("done", result)
                self.assertEqual(["test_or_dut", route, "summarize"], calls)

                skip_path = os.path.join(run_dir, "nodes", skipped_node, "skip.json")
                with open(skip_path, "r") as handle:
                    skipped = json.load(handle)
                self.assertEqual("skipped", skipped["status"])
                self.assertEqual(route, skipped["skip"]["actual"])
                self.assertEqual(skipped_node, skipped["skip"]["expected"])

                summary_input = os.path.join(run_dir, "nodes", "summarize", "attempt-1", "input.json")
                with open(summary_input, "r") as handle:
                    upstream = json.load(handle)["upstream"]
                self.assertEqual([route], sorted(upstream))

                with open(os.path.join(run_dir, "trace.jsonl"), "r") as handle:
                    trace = [json.loads(line) for line in handle if line.strip()]
                selected = [event for event in trace if event["event"] == "route_selected"]
                self.assertEqual(1, len(selected))
                self.assertEqual(route, selected[0]["target"])

    def test_unknown_route_halts_before_any_debug_branch_runs(self):
        result, calls, run_dir = self._run("unknown_debug", "run-invalid")
        self.assertEqual("halted", result)
        self.assertEqual(["test_or_dut"], calls)
        with open(os.path.join(run_dir, "halt.json"), "r") as handle:
            halt = json.load(handle)
        self.assertEqual("unmatched_route", halt["reason"])
        self.assertEqual("unknown_debug", halt["route"]["actual"])
        self.assertEqual(["ifu_debug", "lsu_debug"], halt["route"]["expected"])

    def test_recover_restores_success_and_skipped_nodes(self):
        result, _calls, run_dir = self._run("lsu_debug", "run-recover")
        self.assertEqual("done", result)
        state, histories = engine.recover(_workflow(), run_dir)
        self.assertEqual("success", state["test_or_dut"]["status"])
        self.assertEqual("success", state["lsu_debug"]["status"])
        self.assertEqual("skipped", state["ifu_debug"]["status"])
        self.assertEqual("success", state["summarize"]["status"])
        self.assertEqual([], histories["ifu_debug"])

    def test_when_contract_is_static_and_references_declared_route_data(self):
        self.assertEqual([], validate_workflow(_workflow(), self.directory))

        missing_need = _workflow()
        missing_need["nodes"][1]["needs"] = []
        self.assertTrue(any("when.node: must also appear in needs" in error for error in validate_workflow(missing_need, self.directory)))

        missing_field = _workflow()
        missing_field["nodes"][0]["output_schema"] = {"evidence": "string"}
        self.assertTrue(any("source output_schema must declare route: string" in error for error in validate_workflow(missing_field, self.directory)))


class StandaloneBranchRoutingTest(unittest.TestCase):
    def test_built_artifact_routes_test_or_dut_to_lsu_or_ifu(self):
        directory = tempfile.mkdtemp(prefix="camflow-branch-artifact-")
        artifact = os.path.join(directory, "camflow")
        workflow = os.path.join(directory, "workflow.yaml")
        executor = os.path.join(directory, "executor.py")
        try:
            skill = os.path.join(directory, "skills", "local")
            os.makedirs(skill)
            with open(os.path.join(skill, "SKILL.md"), "w") as handle:
                handle.write("# local\n")
            with open(workflow, "w") as handle:
                handle.write("""workflow: test_or_dut_route
version: "1.2"
nodes:
  -
    id: test_or_dut
    goal: classify debug domain
    steps:
      - choose LSU or IFU
    run:
      skill: local
    output_schema:
      route: string
      evidence: string
  -
    id: lsu_debug
    goal: debug LSU
    steps:
      - inspect LSU
    needs:
      - test_or_dut
    when:
      node: test_or_dut
      path: data.route
      equals: lsu_debug
    run:
      skill: local
  -
    id: ifu_debug
    goal: debug IFU
    steps:
      - inspect IFU
    needs:
      - test_or_dut
    when:
      node: test_or_dut
      path: data.route
      equals: ifu_debug
    run:
      skill: local
""")
            with open(executor, "w") as handle:
                handle.write("""from __future__ import print_function
import json
import os

node = os.path.basename(os.path.dirname(os.getcwd()))
data = {}
if node == "test_or_dut":
    data = {"route": os.environ["TEST_ROUTE"], "evidence": "fixture"}
with open("agent_output.json", "w") as output:
    json.dump({"status": "success", "data": data, "error": None, "feedback": None, "request_human": False}, output)
""")
            build_env = os.environ.copy()
            build_env["PYTHONUTF8"] = "1"
            subprocess.check_call(
                [sys.executable, "build_camflow.py", "--output", artifact],
                cwd=ROOT,
                env=build_env,
            )
            executor_command = '"%s" "%s"' % (
                sys.executable.replace("\\", "/"),
                executor.replace("\\", "/"),
            )
            for route, skipped_node in (("lsu_debug", "ifu_debug"), ("ifu_debug", "lsu_debug")):
                run_dir = os.path.join(directory, "run-" + route)
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["CAMFLOW_EXECUTOR"] = executor_command
                env["TEST_ROUTE"] = route
                code = subprocess.call(
                    [sys.executable, artifact, "run", workflow, "--out", run_dir],
                    env=env,
                )
                self.assertEqual(0, code)
                self.assertTrue(os.path.isfile(os.path.join(run_dir, "nodes", route, "attempt-1", "output.json")))
                self.assertTrue(os.path.isfile(os.path.join(run_dir, "nodes", skipped_node, "skip.json")))
                self.assertFalse(os.path.isdir(os.path.join(run_dir, "nodes", skipped_node, "attempt-1")))
        finally:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
