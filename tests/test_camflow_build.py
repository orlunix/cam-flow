from __future__ import print_function

import os
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
