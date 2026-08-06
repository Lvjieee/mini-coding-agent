from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.run_eval import ARMS, TASKS, build_loop
from harness import ApprovalGate, AuditLog, CompletionDefense, FileGuard, Policy, SensorBank, ToolContext


class RecordingClient:
    """记录是否被调用——用来确认验收器该跑的时候跑、该关的时候不跑。"""

    def __init__(self, reply: str = '{"verdict": "accept", "issues": []}'):
        self.calls = 0
        self.reply = reply

    def complete(self, messages, tools):
        from harness import Message
        self.calls += 1
        return Message("assistant", content=self.reply)


def make_ctx(workspace: str) -> ToolContext:
    return ToolContext(
        policy=Policy(workspace_root=workspace),
        approval=ApprovalGate(interactive=False),
        audit=AuditLog(os.path.join(workspace, ".harness", "audit.jsonl")),
        guard=FileGuard(),
        workspace=workspace,
    )


class UseEvaluatorFlagTests(unittest.TestCase):
    """client 总会传给防线，所以关闭验收器必须靠显式开关，不能靠"不传 client"。"""

    def test_evaluator_runs_when_enabled(self):
        with tempfile.TemporaryDirectory() as workspace:
            client = RecordingClient()
            defense = CompletionDefense(
                sensors=SensorBank(), checklist=None, tool_ctx=make_ctx(workspace),
                client=client, use_evaluator=True)

            problems = defense.check("实现一个函数")

            self.assertEqual(client.calls, 1)
            self.assertEqual(problems, [])

    def test_evaluator_skipped_when_disabled_even_with_client(self):
        with tempfile.TemporaryDirectory() as workspace:
            client = RecordingClient()
            defense = CompletionDefense(
                sensors=SensorBank(), checklist=None, tool_ctx=make_ctx(workspace),
                client=client, use_evaluator=False)

            problems = defense.check("实现一个函数")

            self.assertEqual(client.calls, 0, "sensors 臂不应调用验收器")
            self.assertEqual(problems, [])

    def test_disabled_evaluator_still_reports_sensor_failures(self):
        with tempfile.TemporaryDirectory() as workspace:
            from harness import CommandSensor
            sensors = SensorBank()
            sensors.add(CommandSensor("always-fail", "exit 1", tier="pre_done",
                                      cwd=workspace, timeout=30))
            defense = CompletionDefense(
                sensors=sensors, checklist=None, tool_ctx=make_ctx(workspace),
                client=RecordingClient(), use_evaluator=False)

            self.assertTrue(defense.check("实现一个函数"))


class ArmConfigTests(unittest.TestCase):
    def setUp(self):
        self.task = next(t for t in TASKS if t["name"] == "median")

    def _build(self, arm: str, workspace: str):
        return build_loop(workspace, arm, RecordingClient(), RecordingClient(), self.task)

    def test_baseline_has_no_defense_and_no_sensor(self):
        with tempfile.TemporaryDirectory() as workspace:
            loop, _ = self._build("baseline", workspace)
        self.assertFalse(loop.enable_defense)
        self.assertEqual(len(loop.sensors.run("pre_done")), 0)
        self.assertNotIn("checklist_mark", loop.registry._specs)

    def test_sensors_arm_enables_defense_and_sensor_but_not_evaluator(self):
        with tempfile.TemporaryDirectory() as workspace:
            loop, _ = self._build("sensors", workspace)
            self.assertTrue(loop.enable_defense)
            self.assertEqual(len(loop.sensors.run("pre_done")), 1)
            self.assertFalse(loop.defense.use_evaluator)
            self.assertIsNone(loop.evaluator_client)
            # 没有清单工具 → 清单恒为空 → 该关是 no-op
            self.assertNotIn("checklist_mark", loop.registry._specs)

    def test_defense_arm_enables_every_layer(self):
        with tempfile.TemporaryDirectory() as workspace:
            loop, _ = self._build("defense", workspace)
            self.assertTrue(loop.enable_defense)
            self.assertEqual(len(loop.sensors.run("pre_done")), 1)
            self.assertTrue(loop.defense.use_evaluator)
            self.assertIsNotNone(loop.evaluator_client)
            self.assertIn("checklist_mark", loop.registry._specs)

    def test_arms_form_a_monotonic_ablation_ladder(self):
        """每一臂只增加层、不移除层——否则对比结果无法归因到某一层。"""
        layers = ["defense", "sensor", "checklist", "evaluator"]
        ladder = [[ARMS[arm][layer] for layer in layers]
                  for arm in ("baseline", "sensors", "defense")]
        for weaker, stronger in zip(ladder, ladder[1:]):
            for a, b in zip(weaker, stronger):
                self.assertLessEqual(int(a), int(b))


if __name__ == "__main__":
    unittest.main()
