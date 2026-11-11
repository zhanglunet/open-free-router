# -*- coding: utf-8 -*-
"""PRD-350/T2 测试（trend #1629 的 T2 环节）：路由决策重放包。

运行：.venv/bin/python -m pytest tests/test_prd350_t2_routing_replay.py -q

覆盖（T1 §3.1/§3.2/§4 + R1–R4）：三件套构建（决策缺字段/空快照
PACK_INCOMPLETE）；重放三步（同快照同规则 REPLAY_OK/健康或配额快照漂移或
版本漂移 INPUT_DRIFT 拒绝防当前状态冒充当时/决策漂移 MISMATCH 首分歧条
显形）；R2 零路由切换（AST 零配置写导入零网络——重放只复算判定层）；
R4 账本 append-only；与 PRD-336 fallback 语义划界（重放只读零 settle）。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from routing_replay import (  # noqa: E402
    ERROR_CODES,
    ReplayLedger,
    make_pack,
    replay,
)

NOW = "2026-11-11T09:00:00Z"
HEALTH = {"backend-a（占位）": "healthy", "backend-b（占位）": "degraded"}
QUOTA = {"backend-a（占位）": 0.8, "backend-b（占位）": 0.2}
RECS = [{"backend": "backend-a（占位）", "why": "健康且配额最优（占位）",
         "latency_ms_expected": 120, "cost_expected": 1}]


def rec_fn(health, quota):
    return [dict(r) for r in RECS]


class TestPackBuild(unittest.TestCase):
    def test_pack_three_parts_complete(self):
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
        self.assertEqual(pack["schema"], "routing-replay-pack/v1")
        fp = pack["input_fingerprint"]
        self.assertTrue(fp["health_quota_digest"].startswith("sha16:"))
        self.assertEqual(fp["rules_version"], "v1")
        self.assertEqual(pack["output"]["backends"], ["backend-a（占位）"])
        self.assertEqual(pack["output"]["latency_total"], 120)

    def test_incomplete_refused(self):
        bad = dict(RECS[0])
        del bad["why"]
        self.assertEqual(make_pack([bad], HEALTH, QUOTA,
                                   rules_version="v1")["refused"],
                         "PACK_INCOMPLETE")
        self.assertEqual(make_pack(RECS, {}, QUOTA,
                                   rules_version="v1")["refused"],
                         "PACK_INCOMPLETE")
        self.assertEqual(make_pack(RECS, HEALTH, QUOTA,
                                   rules_version="")["refused"],
                         "PACK_INCOMPLETE")


class TestReplay(unittest.TestCase):
    def _pack(self):
        return make_pack(RECS, HEALTH, QUOTA, rules_version="v1")

    def test_same_snapshots_replay_ok(self):
        out = replay(self._pack(), HEALTH, QUOTA, rec_fn, rules_version="v1")
        self.assertEqual(out["verdict"], "REPLAY_OK")

    def test_input_drift_refused(self):
        """指纹复核：健康/配额/版本漂移 → INPUT_DRIFT 拒绝（防冒充）。"""
        out = replay(self._pack(), {"backend-a（占位）": "degraded"}, QUOTA,
                     rec_fn, rules_version="v1")
        self.assertEqual(out["refused"], "REPLAY_INPUT_DRIFT")
        out2 = replay(self._pack(), HEALTH, {"backend-a（占位）": 0.1},
                      rec_fn, rules_version="v1")
        self.assertEqual(out2["refused"], "REPLAY_INPUT_DRIFT")
        out3 = replay(self._pack(), HEALTH, QUOTA, rec_fn,
                      rules_version="v2")
        self.assertEqual(out3["refused"], "REPLAY_INPUT_DRIFT")

    def test_mismatch_first_divergence_visible(self):
        """R3：决策漂移 → MISMATCH 首分歧条显形，零静默换新路由。"""
        drift_fn = lambda h, q: [dict(RECS[0], backend="backend-b（占位）")]
        out = replay(self._pack(), HEALTH, QUOTA, drift_fn,
                     rules_version="v1")
        self.assertEqual(out["verdict"], "REPLAY_MISMATCH")
        self.assertEqual(out["evidence"]["first_divergence_index"], 0)
        self.assertEqual(out["evidence"]["got"]["backend"], "backend-b（占位）")
        out2 = replay(self._pack(), HEALTH, QUOTA, lambda h, q: [],
                      rules_version="v1")
        self.assertEqual(out2["verdict"], "REPLAY_MISMATCH")
        self.assertEqual(out2["evidence"]["got"], None)

    def test_incomplete_pack_refused(self):
        out = replay({"schema": "routing-replay-pack/v1"}, HEALTH, QUOTA,
                     rec_fn, rules_version="v1")
        self.assertEqual(out["refused"], "PACK_INCOMPLETE")


class TestLedgerAndRedline(unittest.TestCase):
    def test_ledger_append_only(self):
        led = ReplayLedger()
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
        led.record(pack, {"verdict": "REPLAY_OK", "evidence": None}, at=NOW)
        before = [dict(x) for x in led.entries]
        led.record(pack, {"refused": "REPLAY_INPUT_DRIFT", "evidence": None},
                   at=NOW)
        self.assertEqual(led.entries[0], before[0])
        self.assertEqual(len(led.entries), 2)

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"REPLAY_INPUT_DRIFT", "REPLAY_MISMATCH",
                          "PACK_INCOMPLETE"})

    def test_ast_zero_config_write_zero_network(self):
        """R2 AST：零路由切换——零配置写/零网络/零子进程导入。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess",
                  "config", "router_registry"}
        tree = ast.parse((REPO / "src" / "open_free_router" /
                          "routing_replay.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)


if __name__ == "__main__":
    unittest.main()
