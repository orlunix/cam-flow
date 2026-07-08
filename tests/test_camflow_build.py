from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CamflowBuildTest(unittest.TestCase):
    def test_embeds_builtin_planner_workflow_and_skills(self):
        handle, output = tempfile.mkstemp(prefix="camflow-build-")
        os.close(handle)
        os.unlink(output)
        try:
            subprocess.check_call([sys.executable, "build_camflow.py", "--output", output], cwd=ROOT)
            with open(output, "r") as generated:
                text = generated.read()
            self.assertIn("builtin/planner/workflow.yaml", text)
            self.assertIn("prompt_analyzer", text)
            self.assertIn("workflow_designer", text)
            self.assertIn("yaml_writer", text)
        finally:
            if os.path.exists(output):
                os.unlink(output)


if __name__ == "__main__":
    unittest.main()

class YamlRoundTripTest(unittest.TestCase):
    def test_numeric_looking_version_remains_string(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from camflow_pkg.yaml_lite import dumps, loads
        self.assertEqual("1.2", loads(dumps({"version": "1.2"}))["version"])

class ReadableArtifactTest(unittest.TestCase):
    def test_embedded_assets_are_emitted_as_readable_source_lines(self):
        handle, output = tempfile.mkstemp(prefix="camflow-readable-")
        os.close(handle)
        os.unlink(output)
        try:
            subprocess.check_call([sys.executable, "build_camflow.py", "--output", output], cwd=ROOT)
            with open(output, "r") as generated:
                lines = generated.readlines()
            self.assertGreater(len(lines), 1000)
            self.assertTrue(any("_EMBEDDED_SKILLS[" in line for line in lines))
            self.assertTrue(any("_EMBEDDED_ASSETS[" in line for line in lines))
        finally:
            if os.path.exists(output):
                os.unlink(output)

class PackageBoundaryTest(unittest.TestCase):
    def test_run_rejects_workflow_with_missing_local_skill(self):
        directory = tempfile.mkdtemp(prefix="camflow-missing-skill-")
        artifact = os.path.join(directory, "camflow")
        workflow = os.path.join(directory, "workflow.yaml")
        input_path = os.path.join(directory, "input.json")
        try:
            subprocess.check_call([sys.executable, "build_camflow.py", "--output", artifact], cwd=ROOT)
            with open(workflow, "w") as handle:
                handle.write("""workflow: strict
version: "1.2"
input_schema:
  case_id: string
nodes:
  -
    id: inspect
    goal: inspect
    steps:
      - inspect
    run:
      skill: analyzer
""")
            with open(input_path, "w") as handle:
                handle.write('{"case_id": "case-1"}\n')
            result = subprocess.Popen([artifact, "run", workflow, "--input", input_path, "--out", os.path.join(directory, "run")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _stdout, stderr = result.communicate()
            self.assertEqual(1, result.returncode)
            self.assertIn(b"local SKILL.md missing", stderr)
        finally:
            shutil.rmtree(directory)

class ResumeTest(unittest.TestCase):
    def test_resume_retries_halted_node_in_same_run_directory(self):
        directory = tempfile.mkdtemp(prefix="camflow-resume-")
        artifact = os.path.join(directory, "camflow")
        workflow = os.path.join(directory, "workflow.yaml")
        skill_dir = os.path.join(directory, "skills", "local")
        input_path = os.path.join(directory, "input.json")
        run_dir = os.path.join(directory, "run")
        executor = os.path.join(directory, "executor")
        try:
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as handle:
                handle.write("# local\n")
            with open(workflow, "w") as handle:
                handle.write("""workflow: resume_case
version: "1.2"
input_schema:
  case_id: string
nodes:
  -
    id: inspect
    goal: inspect
    steps:
      - inspect
    run:
      skill: local
    retry: 0
""")
            with open(input_path, "w") as handle:
                handle.write('{"case_id": "case-1"}\n')
            with open(executor, "w") as handle:
                handle.write("""#!/bin/sh
if [ "$(basename "$PWD")" = "attempt-1" ]; then
  printf '%s\\n' '{"status":"fail","data":{},"error":{"code":"FIRST","message":"first attempt"},"feedback":"retry","request_human":false}' > agent_output.json
else
  printf '%s\\n' '{"status":"success","data":{},"error":null,"feedback":null,"request_human":false}' > agent_output.json
fi
""")
            os.chmod(executor, 0o755)
            subprocess.check_call([sys.executable, "build_camflow.py", "--output", artifact], cwd=ROOT)
            env = os.environ.copy()
            env["CAMFLOW_EXECUTOR"] = executor
            first = subprocess.call([artifact, "run", workflow, "--input", input_path, "--out", run_dir], env=env)
            self.assertEqual(2, first)
            resumed = subprocess.call([artifact, "resume", run_dir], env=env)
            self.assertEqual(0, resumed)
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "nodes", "inspect", "attempt-2", "output.json")))
        finally:
            shutil.rmtree(directory)

class ContractTest(unittest.TestCase):
    def test_verify_requires_exactly_one_supported_form(self):
        directory = tempfile.mkdtemp(prefix="camflow-contract-")
        try:
            skill = os.path.join(directory, "skills", "local")
            os.makedirs(skill)
            with open(os.path.join(skill, "SKILL.md"), "w") as handle:
                handle.write("# local\n")
            sys.path.insert(0, os.path.join(ROOT, "src"))
            from camflow_pkg.contracts import validate_workflow
            spec = {"workflow": "contract", "version": "1.2", "nodes": [{"id": "one", "goal": "one", "steps": ["one"], "run": {"skill": "local"}, "verify": {"criterion": "x", "command": "true"}}]}
            errors = validate_workflow(spec, directory)
            self.assertTrue(any("verify" in error for error in errors))
        finally:
            shutil.rmtree(directory)

class RunFromTest(unittest.TestCase):
    def test_run_from_resets_target_and_downstream_only(self):
        directory = tempfile.mkdtemp(prefix="camflow-run-from-")
        artifact = os.path.join(directory, "camflow")
        workflow = os.path.join(directory, "workflow.yaml")
        input_path = os.path.join(directory, "input.json")
        run_dir = os.path.join(directory, "run")
        executor = os.path.join(directory, "executor")
        try:
            skill = os.path.join(directory, "skills", "local")
            os.makedirs(skill)
            with open(os.path.join(skill, "SKILL.md"), "w") as handle: handle.write("# local\n")
            with open(workflow, "w") as handle:
                handle.write("""workflow: rerun_case
version: "1.2"
input_schema:
  case_id: string
nodes:
  -
    id: first
    goal: first
    steps:
      - first
    run:
      skill: local
  -
    id: second
    goal: second
    steps:
      - second
    needs:
      - first
    run:
      skill: local
""")
            with open(input_path, "w") as handle: handle.write('{"case_id": "case-1"}\n')
            with open(executor, "w") as handle:
                handle.write("#!/bin/sh\nprintf '%s\\n' '{\"status\":\"success\",\"data\":{},\"error\":null,\"feedback\":null,\"request_human\":false}' > agent_output.json\n")
            os.chmod(executor, 0o755)
            subprocess.check_call([sys.executable, "build_camflow.py", "--output", artifact], cwd=ROOT)
            env = os.environ.copy(); env["CAMFLOW_EXECUTOR"] = executor
            self.assertEqual(0, subprocess.call([artifact, "run", workflow, "--input", input_path, "--out", run_dir], env=env))
            self.assertEqual(0, subprocess.call([artifact, "run", "--from", "first", "--run-dir", run_dir], env=env))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "nodes", "first", "attempt-1", "output.json")))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "nodes", "second", "attempt-1", "output.json")))
        finally:
            shutil.rmtree(directory)
