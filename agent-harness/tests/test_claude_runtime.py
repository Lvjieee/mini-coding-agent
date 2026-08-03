from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness.claude_runtime import ClaudeCodeRuntime
from harness.loop import Budget


class FakeDefense:
    def __init__(self, checks):
        self.checks = iter(checks)
        self.checklist = None
        self.audit = None

    def check(self, goal):
        return next(self.checks)


class FakeCompleted:
    def __init__(self, payload, returncode=0, stderr=""):
        self.stdout = json.dumps(payload) if isinstance(payload, dict) else payload
        self.stderr = stderr
        self.returncode = returncode


class ClaudeCodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def runtime(self, checks, **kwargs):
        return ClaudeCodeRuntime(
            self.workspace.name,
            FakeDefense(checks),
            budget=Budget(completion_retries=kwargs.pop("retries", 3)),
            **kwargs,
        )

    @patch("harness.claude_runtime.subprocess.run")
    def test_success_requires_defense(self, run):
        run.return_value = FakeCompleted({
            "is_error": False,
            "session_id": "session-1",
            "num_turns": 2,
            "result": "implemented",
            "modelUsage": {"glm-4-flash": {"outputTokens": 20}},
        })

        result = self.runtime([[]]).run("implement task")

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["cwd"], self.workspace.name)

    @patch("harness.claude_runtime.subprocess.run")
    def test_rejection_resumes_same_session(self, run):
        run.side_effect = [
            FakeCompleted({"session_id": "session-1", "num_turns": 2, "result": "done"}),
            FakeCompleted({"session_id": "session-1", "num_turns": 1, "result": "fixed"}),
        ]
        runtime = self.runtime([["missing test"], []])

        result = runtime.run("implement task")

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["rejections"], 1)
        command = run.call_args_list[1].args[0]
        self.assertIn("--resume", command)
        self.assertIn("session-1", command)
        self.assertIn("尚未达到完成标准", command[2])

    @patch("harness.claude_runtime.subprocess.run")
    def test_cli_error_is_structured(self, run):
        run.return_value = FakeCompleted("rate limit", returncode=1, stderr="quota exceeded")

        result = self.runtime([[]]).run("implement task")

        self.assertEqual(result["status"], "error")
        self.assertIn("quota exceeded", result["summary"])

    @patch("harness.claude_runtime.subprocess.run")
    def test_timeout_is_structured(self, run):
        run.side_effect = subprocess.TimeoutExpired("claude", 1)

        result = self.runtime([[]]).run("implement task")

        self.assertEqual(result["status"], "error")
        self.assertIn("超时", result["summary"])

    @patch("harness.claude_runtime.subprocess.run")
    def test_retry_exhaustion_does_not_deliver(self, run):
        run.return_value = FakeCompleted({
            "session_id": "session-1",
            "num_turns": 1,
            "result": "still incomplete",
        })
        runtime = self.runtime([["missing test"], ["still missing"]], retries=1)

        result = runtime.run("implement task")

        self.assertEqual(result["status"], "completion_retries_exhausted")
        self.assertEqual(result["rejections"], 1)
        self.assertEqual(run.call_count, 2)

    @patch("harness.claude_runtime.subprocess.run")
    def test_malformed_json_is_fail_closed(self, run):
        run.return_value = FakeCompleted("not-json")

        result = self.runtime([[]]).run("implement task")

        self.assertEqual(result["status"], "error")
        self.assertIn("有效 JSON", result["summary"])


if __name__ == "__main__":
    unittest.main()
