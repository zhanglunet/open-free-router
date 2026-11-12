# -*- coding: utf-8 -*-
"""PRD-378/T4 测试（trend #1882 的 T4 环节）：后端异常验证台划界断言。

运行：.venv/bin/python -m pytest tests/test_prd378_t4_backend_verify_bench_boundary.py -q
（仓库根执行）

覆盖（T1 §0/§3，验收「划界断言通过」）：
  ① 模块隔离双向：backend_verify_bench/gates 零导入 reroute_inbox（364
    收件箱——下游零感知）/gateway_recommend+gateway_aggregate（推荐与
    聚合面）/gpu_fallback_gate（336 回填闸门）/routing_replay*（350 重放）
    /availability（探针面——被消费零 import，经 recheck_fn 注入重跑）+
    借鉴词零出现（codex/security-audit，仅代码标识符扫描 docstring 提及
    不算）；
  ② 零外部仓内耦合钉值：bench 静态导入全标准库（gates 只 import 同链
    probe_fingerprint）；零 LLM 零网络零子进程零配置写零路由切换词面
    AST（R2/学习网关只出推荐红线不变）；
  ③ R2 零新增告警 AST+行为：bench 零拨测/推荐词面（recommend/
    aggregate/probe_batch_time 不在标识符面）——复算只经注入 fn；同一
    输入 verify_alert 两遍逐字节一致（确定性复现）；
  ④ R1 拦截行为钉值：UNVERIFIED 不进 verified 分组（不进换路推荐来源）；
  ⑤ R3 八件套封闭钉值：缺件 BENCH_INCOMPLETE+verdict 词表封闭+探针指纹
    与 350 snapshots_digest 同族口径钉值；
  ⑥ R4：BackendBenchLedger 公开方法面封闭+reverify BENCH_STALE；
  ⑦ §3 划界钉值：gateway_recommend 面（recommend）与 reroute_inbox 面
    （ITEM_KINDS 词表）符号不变——推荐归推荐层、验证台零新增告警，
    互不越界。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.backend_verify_bench import (  # noqa: E402
    BENCH_SCHEMA,
    BackendBenchError,
    BackendBenchLedger,
    VERDICTS,
    probe_fingerprint,
    verify_alert,
    verify_alerts,
)
from open_free_router.reroute_inbox import ITEM_KINDS  # noqa: E402

NOW = "2026-11-13T09:00:00Z"
PROBE = {"backend-a（占位）": {"status": "degraded", "latency_ms": 4000}}
ALERT = {"alert_id": "a-001", "backend": "backend-a（占位）",
         "kind": "health_degraded", "status": "degraded",
         "source_ref": "availability/v1#probe[0]（占位）"}

BENCH_MODULES = ["backend_verify_bench.py", "backend_verify_bench_gates.py"]


def recheck_fn(probe_data):
    return [{"backend": b, "status": v["status"]}
            for b, v in (probe_data or {}).items()
            if v.get("status") != "healthy"]


def _imports_of(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _code_names(path: Path) -> set:
    """仅代码标识符（ast.Name/Attribute）——docstring 提及不算引用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class TestModuleIsolation(unittest.TestCase):
    def test_bench_never_imports_inbox_recommend_or_replay(self):
        """验证台面零触碰收件箱/推荐/聚合/回填/重放/探针（探针经注入重跑
        零 import；收件箱是下游零感知）。"""
        for mod in BENCH_MODULES:
            imported = _imports_of(REPO / "src" / "open_free_router" / mod)
            for banned in ("reroute_inbox", "gateway_recommend",
                           "gateway_aggregate", "gpu_fallback_gate",
                           "routing_replay", "routing_replay_gates",
                           "availability"):
                self.assertNotIn(banned, imported, f"{mod} 导入 {banned}")
            names = _code_names(REPO / "src" / "open_free_router" / mod)
            for symbol in ("InboxLedger", "recommend", "take_snapshot",
                           "make_pack", "probe_batch_time"):
                self.assertNotIn(symbol, names, f"{mod} 引用 {symbol}")

    def test_inbox_never_imports_bench(self):
        """收件箱面零导入验证台（364 不感知本条——双向隔离）。"""
        imported = _imports_of(REPO / "src" / "open_free_router"
                               / "reroute_inbox.py")
        for mod in ("backend_verify_bench", "backend_verify_bench_gates"):
            self.assertNotIn(mod, imported)

    def test_upstream_zero_symbols(self):
        """借鉴仓（codex-security Apache-2.0/security-audit-skill MIT 实测）
        零符号——只借控制面形态（仅代码标识符扫描）。"""
        for mod in BENCH_MODULES:
            names = {n.lower() for n in
                     _code_names(REPO / "src" / "open_free_router" / mod)}
            for word in ("codex", "audit_skill"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")

    def test_zero_internal_coupling_and_redline_ast(self):
        """零外部仓内耦合钉值：bench 静态导入全标准库（gates 只 import 同链
        probe_fingerprint）；零 LLM 零网络零子进程零配置写零路由切换词面。"""
        imported = _imports_of(REPO / "src" / "open_free_router"
                               / "backend_verify_bench.py")
        self.assertLessEqual(imported - {"__future__", "copy", "hashlib",
                                         "json", "pathlib", "typing"},
                             set(), imported)
        gates_imported = _imports_of(
            REPO / "src" / "open_free_router" / "backend_verify_bench_gates.py")
        self.assertLessEqual(
            gates_imported - {"__future__", "typing", "open_free_router"},
            set())
        tree = ast.parse((REPO / "src" / "open_free_router"
                          / "backend_verify_bench_gates.py")
                         .read_text(encoding="utf-8"))
        from_mods = {n.module for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom) and n.module}
        self.assertIn("open_free_router.backend_verify_bench", from_mods,
                      from_mods)
        for mod in BENCH_MODULES:
            names = {n.lower() for n in
                     _code_names(REPO / "src" / "open_free_router" / mod)}
            for word in ("write_config", "switch_route", "reload_now"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")


class TestRedLinePins(unittest.TestCase):
    def test_r2_no_new_alert_ast_and_deterministic(self):
        """R2 AST+行为：bench 零拨测/推荐词面（复算只经注入 fn）；同一输入
        verify_alert 两遍逐字节一致（确定性复现）。"""
        for mod in BENCH_MODULES:
            names = {n.lower() for n in
                     _code_names(REPO / "src" / "open_free_router" / mod)}
            for word in ("recommend", "aggregate", "probe_batch_time"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")
        a = verify_alert(ALERT, PROBE, recheck_fn, at=NOW)
        b = verify_alert(ALERT, PROBE, recheck_fn, at=NOW)
        self.assertEqual(a, b)

    def test_r1_unverified_blocked_at_outlet(self):
        """R1 拦截钉值：UNVERIFIED 不进 verified 分组（不进换路推荐来源）。"""
        out = verify_alerts([dict(ALERT, backend="backend-x（占位）")],
                            PROBE, recheck_fn, at=NOW)
        self.assertEqual(out["verified"], [])
        self.assertEqual(out["unverified_count"], 1)

    def test_r3_eight_piece_closed_and_probe_fp_family(self):
        """R3 钉值：账目缺件 BENCH_INCOMPLETE；verdict 词表封闭；探针指纹
        与 350 snapshots_digest 同族口径（sha16: 前缀确定性）。"""
        led = BackendBenchLedger(None)
        with self.assertRaisesRegex(BackendBenchError, "BENCH_INCOMPLETE"):
            led.record({"schema": BENCH_SCHEMA}, at=NOW)
        self.assertEqual(set(VERDICTS), {"VERIFIED", "UNVERIFIED"})
        fp = probe_fingerprint(PROBE)
        self.assertTrue(fp.startswith("sha16:"))
        self.assertEqual(fp, probe_fingerprint(PROBE))

    def test_r4_ledger_surface_closed_and_stale(self):
        """R4：BackendBenchLedger 公开方法面封闭（零删除方法）+reverify
        探针变更 BENCH_STALE 拒绝复用。"""
        led = BackendBenchLedger(None)
        methods = {n for n in dir(led) if not n.startswith("_")}
        self.assertLessEqual(methods, {"record", "entries", "latest_for",
                                       "reverify"}, methods)
        led.record(verify_alert(ALERT, PROBE, recheck_fn, at=NOW), at=NOW)
        out = led.reverify("a-001", {"backend-y（占位）": {"status": "ok"}},
                           recheck_fn, at=NOW)
        self.assertEqual(out["refused"], "BENCH_STALE")


class TestDemarcationPins(unittest.TestCase):
    def test_recommend_and_inbox_surfaces_unchanged(self):
        """§3 划界钉值：gateway_recommend 面（recommend）与 reroute_inbox
        面（ITEM_KINDS 词表）符号不变——推荐归推荐层、验证台零新增告警，
        互不越界。"""
        import open_free_router.gateway_recommend as gr
        import open_free_router.reroute_inbox as ri
        self.assertTrue(callable(gr.recommend))
        self.assertEqual(set(ITEM_KINDS),
                         {"reroute_recommendation",
                          "fallback_broken_review"})
        self.assertFalse(hasattr(gr, "verify_alert"),
                         "推荐面不应出现验证台符号")
        self.assertFalse(hasattr(ri, "reverify"),
                         "收件箱面不应出现复验符号")


if __name__ == "__main__":
    unittest.main()
