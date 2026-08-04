from __future__ import annotations

import os
import tempfile
import unittest

from eval.run_eval import TASKS, reliability_metrics, run_regression_baseline, wilson_interval


class PassToPassTests(unittest.TestCase):
    def test_all_regression_judges_pass_on_starter_workspaces(self):
        for task in TASKS:
            with self.subTest(task=task["name"]), tempfile.TemporaryDirectory() as workspace:
                for relative, content in task["files"].items():
                    path = os.path.join(workspace, relative)
                    os.makedirs(os.path.dirname(path) or workspace, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(content)
                passed, detail = run_regression_baseline(workspace, task)
                self.assertTrue(passed, detail)
                self.assertNotIn("_pass_to_pass.py", os.listdir(workspace))


class ReliabilityMetricTests(unittest.TestCase):
    def test_pass_power_k_requires_every_run_for_a_task(self):
        rows = [
            {"task": "a", "verified_pass": True, "false_done": False},
            {"task": "a", "verified_pass": True, "false_done": False},
            {"task": "a", "verified_pass": True, "false_done": False},
            {"task": "b", "verified_pass": True, "false_done": False},
            {"task": "b", "verified_pass": False, "false_done": True},
            {"task": "b", "verified_pass": True, "false_done": False},
        ]

        metrics = reliability_metrics(rows)

        self.assertEqual(metrics["k"], 3)
        self.assertEqual(metrics["pass_at_1"], 5)
        self.assertEqual(metrics["pass_power_k"], 1)
        self.assertEqual(metrics["pass_power_k_total"], 2)
        self.assertEqual(metrics["pass_at_least_1"], 2)

    def test_wilson_interval_for_zero_of_27_has_nonzero_upper_bound(self):
        low, high = wilson_interval(0, 27)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.1246, places=4)

    def test_wilson_interval_for_21_of_27_matches_report(self):
        low, high = wilson_interval(21, 27)
        self.assertAlmostEqual(low, 0.5924, places=4)
        self.assertAlmostEqual(high, 0.8939, places=4)


if __name__ == "__main__":
    unittest.main()
