# -*- coding: utf-8 -*-
"""PRD-201/T4 自测（trend #1008）：固定评估集定期重跑 + 漂移标注。

运行：python3 -m unittest tests.test_prd201_t4_drift -v（仓库根执行）

覆盖（验收「漂移报告可生成」）：
  首跑：no_drift + 说明（留痕不误判）
  与上一轮对比：成功率的 delta 计算；成功率降超关注线 → drift
  延迟/成本超关注线 → drift；阈内 → no_drift
  run 落盘 roundtrip + 按 label 取最近一份（定期重跑留痕）
  阈值可覆盖

虚构纪律：全合成 predict_fn；零网络。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.route_eval_drift import (  # noqa: E402
    load_latest_run,
    run_and_compare,
    save_run,
)
from open_free_router.route_obs import RouteObservatory  # noqa: E402

PROMPTS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9", "p10"]


def make_predict(rate_ok, latency=100.0, cost=0.01):
    def predict(prompt):
        idx = int(prompt[1:])
        ok = idx <= round(rate_ok * len(PROMPTS))
        return {"route_id": "r", "status": "ok" if ok else "failed",
                "latency_ms": latency, "cost_usd": cost}
    return predict


class DriftTests(unittest.TestCase):
    def test_first_run_no_baseline(self):
        obs = RouteObservatory()
        run = run_and_compare(obs, PROMPTS, make_predict(0.9), "weekly")
        self.assertEqual(run["drift"]["verdict"], "no_drift")
        self.assertIn("首跑", run["drift"]["reason"])

    def test_success_rate_drop_flagged(self):
        obs = RouteObservatory()
        base = run_and_compare(obs, PROMPTS, make_predict(0.9), "weekly")
        run = run_and_compare(obs, PROMPTS, make_predict(0.6), "weekly",
                              previous_run=base)
        self.assertEqual(run["drift"]["verdict"], "drift")
        self.assertTrue(any("成功率" in i for i in run["drift"]["drift_items"]))

    def test_within_threshold_no_drift(self):
        obs = RouteObservatory()
        base = run_and_compare(obs, PROMPTS, make_predict(0.9), "weekly")
        run = run_and_compare(obs, PROMPTS, make_predict(0.9, latency=110.0), "weekly",
                              previous_run=base)
        self.assertEqual(run["drift"]["verdict"], "no_drift")

    def test_latency_and_cost_rise_flagged(self):
        obs = RouteObservatory()
        base = run_and_compare(obs, PROMPTS, make_predict(0.9, latency=100.0, cost=0.01),
                               "weekly")
        run = run_and_compare(obs, PROMPTS,
                              make_predict(0.9, latency=200.0, cost=0.02), "weekly",
                              previous_run=base)
        self.assertEqual(run["drift"]["verdict"], "drift")
        items = "；".join(run["drift"]["drift_items"])
        self.assertIn("延迟", items)
        self.assertIn("成本", items)

    def test_save_and_load_latest(self):
        obs = RouteObservatory()
        run = run_and_compare(obs, PROMPTS, make_predict(0.9), "weekly")
        with tempfile.TemporaryDirectory() as td:
            save_run(run, Path(td))
            loaded = load_latest_run(Path(td), "weekly")
            self.assertEqual(loaded["label"], "weekly")
            self.assertEqual(loaded["success_rate"], run["success_rate"])
            self.assertIsNone(load_latest_run(Path(td), "daily"))

    def test_thresholds_overridable(self):
        prompts = [f"p{i}" for i in range(1, 51)]  # 50 条：支持 0.04 的细粒度下降

        def make_predict_n(pass_n):
            def predict(prompt):
                ok = int(prompt[1:]) <= pass_n
                return {"route_id": "r", "status": "ok" if ok else "failed",
                        "latency_ms": 100.0, "cost_usd": 0.01}
            return predict

        obs = RouteObservatory()
        base = run_and_compare(obs, prompts, make_predict_n(50), "weekly")
        # 降 0.04：默认阈 0.05 不标，严阈 0.01 标
        relaxed = run_and_compare(obs, prompts, make_predict_n(48), "weekly",
                                  previous_run=base)
        self.assertEqual(relaxed["drift"]["verdict"], "no_drift")
        strict = run_and_compare(obs, prompts, make_predict_n(48), "weekly",
                                 previous_run=base,
                                 thresholds={"success_rate_drop": 0.01})
        self.assertEqual(strict["drift"]["verdict"], "drift")  # 0.04 降 > 0.01 严阈值


if __name__ == "__main__":
    unittest.main(verbosity=2)
