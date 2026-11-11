# -*- coding: utf-8 -*-
"""PRD-350/T4 测试（trend #1629 的 T4 环节）：路由决策重放划界断言。

运行：.venv/bin/python -m pytest tests/test_prd350_t4_routing_replay_boundary.py -q
（仓库根执行）

覆盖（T1 §0/§2，验收「划界断言通过」）：
  ① 模块隔离双向：routing_replay 零导入 config_snapshot（PRD-280 配置
    可逆）/route_obs（PRD-201 看板）/harness_route（PRD-257 协议层）+
    借鉴词零出现（AiSOC/OpenBot MIT 只借形态）；config_snapshot 零导入
    routing_replay（快照面不感知重放）；routing_replay_gates 只导入同链
    routing_replay（T3 消费 T2 同 PRD 内合法耦合钉值）；
  ② 唯一耦合钉值：routing_replay 静态导入全标准库——推荐层经 recommend_fn
    注入（零 import 耦合）；零网络零子进程零配置写词面 AST（R2）；
  ③ R1 行为钉值：决策缺 backend/why 任一或空 recommendations →
    refused=PACK_INCOMPLETE；
  ④ R2 零路由切换行为钉值：recommend_fn 收到深拷贝（health/quota 零污染）
    +重放返回值零当前路由写面；
  ⑤ R3 零静默钉值：mismatch 键面封闭 {schema,refused,verdict,evidence,
    rerun_count}——零回退新路由载荷键；
  ⑥ R4：ReplayLedger 方法面封闭（零删除方法）；
  ⑦ §2 划界钉值：config_snapshot 快照面（route-config-snapshot-v1 +
    FingerprintMismatch）符号不变——PRD-280 管配置可逆，本条管路由可重算，
    互不越界。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from open_free_router.config_snapshot import (  # noqa: E402
    FingerprintMismatch,
    SNAPSHOT_SCHEMA,
)
from open_free_router.routing_replay import (  # noqa: E402
    ReplayLedger,
    make_pack,
    replay,
)

HEALTH = {"backend-a（占位）": "healthy", "backend-b（占位）": "degraded"}
QUOTA = {"backend-a（占位）": 0.8, "backend-b（占位）": 0.2}
RECS = [{"backend": "backend-a（占位）", "why": "健康且配额最优（占位）",
         "latency_ms_expected": 120, "cost_expected": 1}]

ROUTER = Path(__file__).resolve().parents[1] / "src" / "open_free_router"
REPLAY_MODULES = ["routing_replay.py", "routing_replay_gates.py"]


def rec_fn(health, quota):
    return [dict(r) for r in RECS]


def _imports_of(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


class TestModuleIsolation(unittest.TestCase):
    def test_replay_never_imports_snapshot_obs_or_route(self):
        """重放面零触碰配置快照/看板/协议层（可重算不感知配置可逆与转发）。"""
        imported = _imports_of(ROUTER / "routing_replay.py")
        for banned in ("config_snapshot", "route_obs", "harness_route",
                       "route_board"):
            self.assertNotIn(banned, imported, f"routing_replay 导入 {banned}")
        blob = ast.dump(ast.parse(
            (ROUTER / "routing_replay.py").read_text(encoding="utf-8")))
        for symbol in ("FingerprintMismatch", "take_snapshot",
                       "fingerprint_routes"):
            self.assertNotIn(symbol, blob, f"routing_replay 引用 {symbol}")

    def test_snapshot_never_imports_replay(self):
        """快照面零导入重放（PRD-280 不感知本条——双向隔离）。"""
        imported = _imports_of(ROUTER / "config_snapshot.py")
        for mod in ("routing_replay", "routing_replay_gates"):
            self.assertNotIn(mod, imported)

    def test_gates_only_coupled_to_same_chain(self):
        """routing_replay_gates 对仓内模块唯一耦合=routing_replay（T3 消费
        T2 同链钉值）。"""
        imported = _imports_of(ROUTER / "routing_replay_gates.py")
        internal = imported - {"__future__", "typing"}
        self.assertEqual(internal, {"routing_replay"}, internal)

    def test_no_aisoc_openbot_symbols(self):
        """借鉴仓（AiSOC/OpenBot，MIT 实测）零符号——只借形态。"""
        for mod in REPLAY_MODULES:
            blob = ast.dump(ast.parse(
                (ROUTER / mod).read_text("utf-8"))).lower()
            for word in ("aisoc", "openbot"):
                self.assertNotIn(word, blob, f"{mod} 出现 {word}")


class TestRedLinePins(unittest.TestCase):
    def test_r1_pack_incomplete_refusal_pinned(self):
        """R1 行为钉值：决策缺 backend/why 或空 recommendations →
        refused=PACK_INCOMPLETE。"""
        out = make_pack([{"backend": "x"}], HEALTH, QUOTA,
                        rules_version="v1")
        self.assertEqual(out["refused"], "PACK_INCOMPLETE")
        out2 = make_pack([], HEALTH, QUOTA, rules_version="v1")
        self.assertEqual(out2["refused"], "PACK_INCOMPLETE")

    def test_r2_zero_route_switch_ast_and_deepcopy(self):
        """R2 AST+行为：零网络零子进程零配置写词面；recommend_fn 收到深拷贝
        （health/quota 零污染——零当前路由写面）。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess",
                  "config_snapshot"}
        for mod in REPLAY_MODULES:
            imported = _imports_of(ROUTER / mod)
            self.assertFalse(imported & banned,
                             f"{mod} 导入 {imported & banned}")
            src = (ROUTER / mod).read_text(encoding="utf-8").lower()
            for word in ("write_config", "apply_route", "switch_route"):
                self.assertNotIn(word, src, f"{mod} 出现 {word}")
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")

        def mutating_fn(health, quota):
            health["backend-a（占位）"] = "就地改写（占位）"
            return [dict(r) for r in RECS]

        replay(pack, HEALTH, QUOTA, mutating_fn, rules_version="v1")
        self.assertEqual(HEALTH["backend-a（占位）"], "healthy",
                         "调用方快照零污染")

    def test_r3_mismatch_returns_closed_keys_no_new_route(self):
        """R3 零静默钉值：mismatch 键面封闭——零回退新路由载荷键。"""
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
        out = replay(pack, HEALTH, QUOTA,
                     lambda h, q: [dict(RECS[0], backend="改写（占位）")],
                     rules_version="v1")
        self.assertEqual(out["verdict"], "REPLAY_MISMATCH")
        self.assertEqual(out["evidence"]["first_divergence_index"], 0)
        self.assertEqual(set(out), {"schema", "refused", "verdict",
                                    "evidence", "rerun_count"})

    def test_r4_ledger_method_surface_closed(self):
        """R4：ReplayLedger 方法面封闭——零删除方法。"""
        led = ReplayLedger()
        methods = {n for n in dir(led) if not n.startswith("_")}
        self.assertLessEqual(methods, {"record", "entries"}, methods)
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
        led.record(pack, {"verdict": "REPLAY_OK", "evidence": None},
                   at="2026-11-11T09:00:00Z")
        self.assertEqual(len(led.entries), 1)


class TestDemarcationPins(unittest.TestCase):
    def test_config_snapshot_surface_unchanged(self):
        """§2 划界钉值：config_snapshot 快照面（route-config-snapshot-v1 +
        FingerprintMismatch + take_snapshot）符号不变——PRD-280 管配置可逆，
        本条管路由可重算，互不越界。"""
        import open_free_router.config_snapshot as cs
        self.assertEqual(SNAPSHOT_SCHEMA, "route-config-snapshot-v1")
        for name in ("FingerprintMismatch", "take_snapshot",
                     "fingerprint_routes"):
            self.assertTrue(hasattr(cs, name), name)
        self.assertFalse(hasattr(cs, "replay"), "快照面不应出现重放符号")


if __name__ == "__main__":
    unittest.main()
