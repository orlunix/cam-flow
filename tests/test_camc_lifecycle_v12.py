from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camflow_pkg import cli, engine


SUCCESS = {
    "status": "success",
    "data": {},
    "error": None,
    "feedback": None,
    "request_human": False,
}


class CamcLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="camflow-camc-")
        self.attempt = os.path.join(self.directory, "nodes", "debug", "attempt-1")
        os.makedirs(self.attempt)
        with open(os.path.join(self.attempt, "agent_output.json"), "w") as handle:
            json.dump(SUCCESS, handle)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_archives_then_stops_and_removes_agent(self):
        calls = []

        def fake_call(command, _cwd, _timeout):
            calls.append(command)
            if command[1] == "run":
                return {"returncode": 0, "stdout": "Starting codex agent abc12345\n", "stderr": ""}
            if command[1] == "archive":
                archive_dir = command[command.index("--output") + 1]
                os.makedirs(archive_dir)
                with open(os.path.join(archive_dir, "abc12345.tar.gz"), "wb") as handle:
                    handle.write(b"archive")
                return {"returncode": 0, "stdout": "archived\n", "stderr": ""}
            if command[1:3] == ["--json", "status"]:
                return {
                    "returncode": 0,
                    "stdout": json.dumps({
                        "id": "abc12345",
                        "session_id": "session-1",
                        "session_binding": "bound",
                        "session_path": "/tmp/rollout.jsonl",
                    }),
                    "stderr": "",
                }
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with mock.patch.object(engine, "_camc_call", side_effect=fake_call):
            with mock.patch.dict(os.environ, {"CAMFLOW_AGENT_TOOL": "codex"}):
                result = engine._invoke(
                    self.directory, self.attempt,
                    {"id": "debug", "run": {"skill": "local"}}, "prompt",
                    flow={"id": "a1b2c3d4", "name": "rvdbg", "label": "rvdbg"},
                )

        self.assertEqual("success", result["status"])
        self.assertNotIn("--auto-exit", calls[0])
        self.assertEqual(["--tool", "codex"], calls[0][2:4])
        self.assertRegex(
            calls[0][calls[0].index("--name") + 1],
            r"^cf-rvdbg-debug-[0-9a-f]{8}$",
        )
        self.assertEqual(
            ["cf-rvdbg", "cf-a1b2c3d4"],
            [calls[0][index + 1] for index, value in enumerate(calls[0]) if value == "--tag"],
        )
        self.assertEqual(
            ["archive", "--json", "stop", "rm"],
            [calls[1][1], calls[2][1], calls[3][1], calls[4][1]],
        )
        with open(os.path.join(self.attempt, "agent.id"), "r") as handle:
            self.assertEqual("abc12345", handle.read().strip())
        with open(os.path.join(self.attempt, "agent.json"), "r") as handle:
            self.assertEqual("bound", json.load(handle)["session_binding"])
        with open(os.path.join(self.attempt, "camc-lifecycle.json"), "r") as handle:
            lifecycle = json.load(handle)
        self.assertEqual(["abc12345.tar.gz"], lifecycle["archive_files"])

    def test_archive_failure_keeps_agent_and_halts_for_inspection(self):
        calls = []

        def fake_call(command, _cwd, _timeout):
            calls.append(command)
            if command[1] == "run":
                return {"returncode": 0, "stdout": "Starting codex agent abc12345\n", "stderr": ""}
            if command[1] == "archive":
                return {"returncode": 1, "stdout": "", "stderr": "session is pending"}
            return {"returncode": 0, "stdout": '{}', "stderr": ""}

        with mock.patch.object(engine, "_camc_call", side_effect=fake_call):
            result = engine._invoke(
                self.directory, self.attempt,
                {"id": "debug", "run": {"skill": "local"}}, "prompt",
            )

        self.assertEqual("fail", result["status"])
        self.assertEqual("CAMC_ARCHIVE_FAILED", result["error"]["code"])
        self.assertTrue(result["request_human"])
        self.assertFalse(any(command[1] in ("stop", "rm") for command in calls))

    def test_archive_success_without_file_keeps_agent(self):
        calls = []

        def fake_call(command, _cwd, _timeout):
            calls.append(command)
            if command[1] == "run":
                return {"returncode": 0, "stdout": "Starting codex agent abc12345\n", "stderr": ""}
            return {"returncode": 0, "stdout": '{}', "stderr": ""}

        with mock.patch.object(engine, "_camc_call", side_effect=fake_call):
            result = engine._invoke(
                self.directory, self.attempt,
                {"id": "debug", "run": {"skill": "local"}}, "prompt",
            )

        self.assertEqual("CAMC_ARCHIVE_FAILED", result["error"]["code"])
        self.assertFalse(any(command[1] in ("stop", "rm") for command in calls))

    def test_rm_failure_is_reported_after_durable_archive(self):
        calls = []

        def fake_call(command, _cwd, _timeout):
            calls.append(command)
            if command[1] == "run":
                return {"returncode": 0, "stdout": "Starting codex agent abc12345\n", "stderr": ""}
            if command[1] == "archive":
                archive_dir = command[command.index("--output") + 1]
                os.makedirs(archive_dir)
                with open(os.path.join(archive_dir, "abc12345.tar.gz"), "wb") as handle:
                    handle.write(b"archive")
                return {"returncode": 0, "stdout": "", "stderr": ""}
            if command[1] == "rm":
                return {"returncode": 1, "stdout": "", "stderr": "remove failed"}
            return {"returncode": 0, "stdout": '{}', "stderr": ""}

        with mock.patch.object(engine, "_camc_call", side_effect=fake_call):
            result = engine._invoke(
                self.directory, self.attempt,
                {"id": "debug", "run": {"skill": "local"}}, "prompt",
            )

        self.assertEqual("CAMC_CLEANUP_FAILED", result["error"]["code"])
        self.assertTrue(result["request_human"])

    def test_verifier_inherits_the_same_flow_identity(self):
        evaluator = os.path.join(self.directory, "skills", "evaluator")
        os.makedirs(evaluator)
        with open(os.path.join(evaluator, "SKILL.md"), "w") as handle:
            handle.write("# evaluator\n")
        flow = {"id": "a1b2c3d4", "name": "rvdbg", "label": "rvdbg"}
        captured = []

        def fake_invoke(_root, _attempt, node, _prompt, flow=None):
            captured.append((node["id"], flow))
            return {
                "status": "success",
                "data": {"approved": True, "reasoning": "ok"},
                "error": None,
                "feedback": None,
                "request_human": False,
            }

        with mock.patch.object(engine, "_invoke", side_effect=fake_invoke):
            passed, reason = engine._agent_verify(
                self.directory,
                {"id": "debug"},
                dict(SUCCESS),
                self.attempt,
                "approve",
                flow=flow,
            )

        self.assertTrue(passed)
        self.assertEqual("agent verifier approved", reason)
        self.assertEqual([("debug-verify", flow)], captured)


class RunSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="camflow-snapshot-")
        self.skill = os.path.join(self.directory, "skills", "local")
        os.makedirs(self.skill)
        with open(os.path.join(self.skill, "SKILL.md"), "w") as handle:
            handle.write("# local\n")
        self.workflow = os.path.join(self.directory, "workflow.yaml")
        with open(self.workflow, "w") as handle:
            handle.write(
                'workflow: snapshot\nversion: "1.2"\nnodes:\n'
                '  -\n    id: one\n    goal: one\n    steps:\n'
                '      - one\n    run:\n      skill: local\n'
                '    verify:\n      command: "true"\n'
            )
        self.input_path = os.path.join(self.directory, "input.json")
        with open(self.input_path, "w") as handle:
            handle.write('{"case_id": "case-1"}\n')

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_snapshot_hashes_are_checked_on_replay(self):
        run_dir = os.path.join(self.directory, "run")
        spec, root = cli._load_workflow(self.workflow)
        cli._snapshot_run(spec, root, self.workflow, run_dir, self.input_path)
        with open(os.path.join(run_dir, "run.json"), "r") as handle:
            flow = json.load(handle)["flow"]
        self.assertEqual("snapshot", flow["name"])
        self.assertEqual("snapshot", flow["label"])
        self.assertRegex(flow["id"], r"^[0-9a-f]{8}$")
        self.assertEqual(
            ["cf-snapshot", "cf-" + flow["id"]],
            flow["tags"],
        )
        input_snapshot = os.path.join(run_dir, "input.json")
        with open(input_snapshot, "rb") as handle:
            original_input = handle.read()

        with mock.patch.object(engine, "_invoke", return_value=dict(SUCCESS)):
            with mock.patch.object(engine, "_verify", return_value=(True, "verified")):
                self.assertEqual(
                    "done",
                    engine.execute(spec, run_dir, run_dir, {"case_id": "case-1"}),
                )
        with open(input_snapshot, "rb") as handle:
            self.assertEqual(original_input, handle.read())

        loaded, _root, data = cli._load_run(run_dir)
        self.assertEqual("snapshot", loaded["workflow"])
        self.assertEqual({"case_id": "case-1"}, data)

        with open(os.path.join(run_dir, "workflow.yaml"), "a") as handle:
            handle.write("# changed\n")
        with self.assertRaisesRegex(ValueError, "differs from the recorded run snapshot"):
            cli._load_run(run_dir)

    def test_long_flow_and_node_names_stay_short_and_collision_resistant(self):
        identity = engine._flow_identity(
            {"workflow": "very_long_riscv_debug_workflow_name"},
            self.directory,
            {"id": "1234abcd", "name": "very_long_riscv_debug_workflow_name"},
        )
        self.assertLessEqual(len(identity["label"]), 12)
        name = engine._camc_agent_name(
            identity,
            "investigate_an_extremely_long_pipeline_component_name",
            self.attempt_path("long"),
        )
        self.assertLessEqual(len(name), 43)
        self.assertRegex(name, r"^cf-[a-z0-9-]+-[a-z0-9-]+-[0-9a-f]{8}$")

    def attempt_path(self, name):
        return os.path.join(self.directory, "nodes", name, "attempt-1")

    def test_fresh_run_refuses_nonempty_run_directory(self):
        run_dir = os.path.join(self.directory, "run")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "old.txt"), "w") as handle:
            handle.write("old\n")

        with mock.patch.object(cli, "execute") as execute:
            code = cli._fresh_run(self.workflow, None, run_dir, None)

        self.assertEqual(1, code)
        self.assertFalse(execute.called)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "old.txt")))


if __name__ == "__main__":
    unittest.main()
