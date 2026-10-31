# -*- coding: utf-8 -*-
"""PRD-202/T5 自测（trend #1014）：验收试运行——全链路演练记录完整。

运行：python3 -m unittest tests.test_prd202_t5_acceptance -v（仓库根执行）

覆盖（验收「全链路演练记录完整」）：
  演练：解析意图 → 回显确认（未确认拒绝）→ 确认切换（trace 落）→ 一键回滚
    （rollback trace 落）→ 再切回 → 全程 observatory trace 齐全且每步可复核
  演练记录 JSON 可落盘（runbook 证据）
虚构纪律：全合成路由；零网络。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.model_switch import (  # noqa: E402
    SwitchExecutor,
    parse_intent,
    echo_confirm,
    undo_switch,
)
from open_free_router.route_obs import RouteObservatory  # noqa: E402


class FullChainTests(unittest.TestCase):
    def test_full_rehearsal(self):
        obs = RouteObservatory()
        ex = SwitchExecutor(obs)
        log = []

        def switch(target):
            log.append(f"switch->{target}")
            return "switched"

        # 1. 解析意图
        intent = parse_intent("换成便宜的")
        self.assertEqual(intent["action"], "switch")
        log.append({"step": "parse", "intent": intent})
        # 2. 回显确认（未确认前零动作）
        confirmation = echo_confirm(intent, "prov-a/m-1",
                                    {"cheap": {"cost": 0.008}})
        self.assertFalse(confirmation["confirmed"])
        log.append({"step": "echo", "confirmation": confirmation})
        rejected = ex.execute({"action": "switch", "confirmed": False},
                              "prov-a/m-1", switch, request_id="req-0")
        self.assertFalse(rejected["ok"])                       # 未确认零动作
        # 3. 确认切换
        result = ex.execute({"action": "switch", "target_route": "prov-b/m-2",
                             "confirmed": True}, "prov-a/m-1", switch,
                            request_id="req-1")
        self.assertTrue(result["ok"])
        log.append({"step": "switch", "result": result})
        # 4. 一键回滚
        undo = undo_switch(ex, "prov-b/m-2", switch, request_id="req-2")
        self.assertTrue(undo["ok"])
        self.assertEqual(undo["to_route"], "prov-a/m-1")
        log.append({"step": "undo", "result": undo})
        # 5. 再切回
        result2 = ex.execute({"action": "switch", "target_route": "prov-b/m-2",
                              "confirmed": True}, "prov-a/m-1", switch,
                             request_id="req-3")
        self.assertTrue(result2["ok"])
        # 6. 演练记录完整：4 条 trace（1 拒绝不落 trace + 3 落），每条可复核
        self.assertEqual(len(obs.traces), 3)
        self.assertEqual([t["route_id"] for t in obs.traces],
                         ["model-switch", "model-switch-rollback", "model-switch"])
        log.append({"step": "traces", "count": len(obs.traces)})
        json.dumps(log, ensure_ascii=False)                    # 演练记录可落盘


if __name__ == "__main__":
    unittest.main(verbosity=2)
