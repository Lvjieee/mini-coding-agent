from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

# 让 tests 目录能 import eval 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval import swebench_adapter as swe


def make_git_repo(directory: str) -> None:
    """在给定目录初始化一个仓库，落一个基线 commit——用来验证 extract_patch。"""
    subprocess.run(["git", "init", "-q", "-b", "base"], cwd=directory, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=directory, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=directory, check=True)
    with open(os.path.join(directory, "a.py"), "w") as f:
        f.write("def add(a, b):\n    return a - b\n")  # 故意留错
    subprocess.run(["git", "add", "."], cwd=directory, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=directory, check=True)


class FilesChangedTests(unittest.TestCase):
    def test_extracts_paths_from_multi_file_diff(self):
        patch = (
            "diff --git a/pkg/a.py b/pkg/a.py\n"
            "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@\n-x\n+y\n"
            "diff --git a/pkg/b.py b/pkg/b.py\n"
            "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@\n-x\n+y\n"
        )
        self.assertEqual(swe.files_changed(patch), ["pkg/a.py", "pkg/b.py"])

    def test_empty_patch_returns_empty(self):
        self.assertEqual(swe.files_changed(""), [])
        self.assertEqual(swe.files_changed(None), [])


class PickEasyInstancesTests(unittest.TestCase):
    def _make(self, iid: str, repo: str, patch: str, test_patch: str = "") -> dict:
        return {"instance_id": iid, "repo": repo,
                "patch": patch, "test_patch": test_patch}

    def test_prefers_single_file_shorter_patch_and_avoids_heavy_repos(self):
        rows = [
            self._make("django__django-1", "django/django",
                       "diff --git a/tiny.py b/tiny.py\n" * 1),   # heavy → 排除
            self._make("flask__flask-1", "pallets/flask",
                       "diff --git a/x.py b/x.py\n" + "diff --git a/y.py b/y.py\n" +
                       "diff --git a/z.py b/z.py\n"),             # 3 文件 → 超上限
            self._make("sympy__sympy-1", "sympy/sympy",
                       "diff --git a/one.py b/one.py\nAAAAA\n"),  # 1 文件 长 patch
            self._make("pytest__pytest-1", "pytest-dev/pytest",
                       "diff --git a/one.py b/one.py\n"),         # 1 文件 短 patch
        ]
        picked = swe.pick_easy_instances(rows, n=2, max_files=2)
        self.assertEqual([r["instance_id"] for r in picked],
                         ["pytest__pytest-1", "sympy__sympy-1"])


class ExtractPatchTests(unittest.TestCase):
    def test_captures_modification_and_new_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            make_git_repo(workspace)

            # Agent 修 bug（改已有文件）+ 新增一个辅助文件
            with open(os.path.join(workspace, "a.py"), "w") as f:
                f.write("def add(a, b):\n    return a + b\n")
            with open(os.path.join(workspace, "notes.txt"), "w") as f:
                f.write("hello\n")

            patch = swe.extract_patch(workspace)
            self.assertIn("diff --git a/a.py b/a.py", patch)
            self.assertIn("return a + b", patch)
            self.assertIn("notes.txt", patch)
            # 抽 patch 不应污染 HEAD——工作区仍在原始 commit 上
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace,
                capture_output=True, text=True, check=True).stdout.strip()
            log = subprocess.run(
                ["git", "log", "--oneline"], cwd=workspace,
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(len(log.splitlines()), 1, "抽 patch 不应产生额外 commit")
            self.assertTrue(head)


class WritePredictionsTests(unittest.TestCase):
    def test_writes_jsonl_and_skips_empty_patches(self):
        rows = [
            {"instance_id": "a-1", "patch": "diff --git a/x.py b/x.py\n"},
            {"instance_id": "a-2", "patch": ""},          # 应被过滤
            {"instance_id": "a-3", "patch": "   \n"},     # 空白也应保留（不判空白）
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "preds.jsonl")
            swe.write_predictions(rows, path, model_name="mini-coding-agent")
            with open(path) as f:
                lines = [json.loads(line) for line in f if line.strip()]
        ids = [row["instance_id"] for row in lines]
        self.assertEqual(ids, ["a-1", "a-3"])
        self.assertEqual(lines[0]["model_name_or_path"], "mini-coding-agent")
        self.assertEqual(lines[0]["model_patch"], "diff --git a/x.py b/x.py\n")


class ParseReportsTests(unittest.TestCase):
    def test_reads_resolved_and_test_status(self):
        with tempfile.TemporaryDirectory() as logs:
            instance_dir = os.path.join(logs, "mini-coding-agent", "sympy__sympy-1")
            os.makedirs(instance_dir)
            with open(os.path.join(instance_dir, "report.json"), "w") as f:
                json.dump({"sympy__sympy-1": {
                    "resolved": True,
                    "patch_successfully_applied": True,
                    "tests_status": {
                        "FAIL_TO_PASS": {"success": ["test_a"], "failure": []},
                        "PASS_TO_PASS": {"success": ["test_b"], "failure": []},
                    },
                }}, f)
            # 一个损坏的报告——应该被容忍跳过
            broken = os.path.join(logs, "mini-coding-agent", "broken")
            os.makedirs(broken)
            with open(os.path.join(broken, "report.json"), "w") as f:
                f.write("{not json")

            reports = swe.parse_reports(logs, "mini-coding-agent")
            self.assertIn("sympy__sympy-1", reports)
            self.assertTrue(reports["sympy__sympy-1"]["resolved"])
            self.assertTrue(reports["sympy__sympy-1"]["patch_applied"])
            self.assertNotIn("broken", reports)


if __name__ == "__main__":
    unittest.main()
