# -*- coding: utf-8 -*-
"""PRD-364/T4 测试（trend #1643 的 T4 环节）：换路推荐收件箱划界断言。

运行：.venv/bin/python -m pytest tests/test_prd364_t4_reroute_inbox_boundary.py -q
（仓库根执行）

覆盖（T1 §0/§2，验收「划界断言通过」）：
  ① 模块隔离双向：reroute_inbox/reroute_inbox_gates 零导入
    gateway_recommend（学习网关——只出推荐红线不变，零 import）/
    gateway_aggregate（聚合面）/gpu_fallback_gate（PRD-336 回填闸门——
    settle 语义同族但零 import）/routing_replay*（PRD-350 重放——快照
    同源模块隔离）+ 借鉴词零出现（plannotator/t3code 只借控制面形态）；
    gateway_recommend 零导入 reroute_inbox*（仅代码标识符扫描，docstring
    提及不算）；
  ② 零网络零子进程零配置写词面 AST（R1）；
  ③ R1 零自动执行 AST+行为：approve 携带 closed 词表配置载荷
    （config/reload/apply）→ INBOX_AUTO_EXEC_ATTEMPT 显形拒绝；裁决
    返回值零配置写/路由切换载荷键；
  ④ R2 只读汇聚行为钉值：collect 注入式双源齐收
    （reroute_recommendation/fallback_broken_review 词表封闭），零内核
    调用面；
  ⑤ R3 每项带证据钉值：推荐项缺 snapshot_digest/recommendation/reason
    任一 → INBOX_ITEM_INCOMPLETE 显形拒绝入箱；
  ⑥ R4：InboxLedger 方法面封闭（零删除方法）+reject 留痕钉值；
  ⑦ §2 划界钉值：gpu_fallback_gate 面（回填闸门符号）不变——回填归
    336、收件箱零配置写，互不越界。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from open_free_router.reroute_inbox import (  # noqa: E402
    InboxError,
    InboxLedger,
    ITEM_KINDS,
    collect,
)

NOW = "2026-11-11T09:00:00Z"
RECS = [{"source_ref": "routing-replay-pack/v1（占位）",
         "snapshot_digest": "sha16:snap（占位）",
         "recommendation": "backend-a → backend-b（占位）",
         "reason": "健康+配额最优（占位）", "recommended_at": NOW}]
BROKEN = [{"event_index": 0, "snapshot_digest": "sha16:snap2（占位）",
           "detail": "双失败（占位）"}]

ROUTER = SRC / "open_free_router"
INBOX_MODULES = ["reroute_inbox.py", "reroute_inbox_gates.py"]


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
    def test_inbox_never_imports_recommend_fallback_or_replay(self):
        """收件箱面零触碰推荐网关/聚合/回填闸门/重放（只读消费=注入式；
        学习网关只出推荐红线不变）。"""
        for mod in INBOX_MODULES:
            imported = _imports_of(ROUTER / mod)
            for banned in ("gateway_recommend", "gateway_aggregate",
                           "gpu_fallback_gate", "routing_replay",
                           "routing_replay_gates"):
                self.assertNotIn(banned, imported, f"{mod} 导入 {banned}")
            names = _code_names(ROUTER / mod)
            for symbol in ("recommend", "REFUSAL_CODES", "make_pack"):
                self.assertNotIn(symbol, names, f"{mod} 引用 {symbol}")

    def test_gateway_never_imports_inbox(self):
        """推荐面零导入收件箱（学习网关不感知本条——双向隔离）。"""
        imported = _imports_of(ROUTER / "gateway_recommend.py")
        for mod in ("reroute_inbox", "reroute_inbox_gates"):
            self.assertNotIn(mod, imported)

    def test_no_plannotator_t3code_symbols(self):
        """借鉴仓（plannotator Apache-2.0/t3code MIT 实测）零符号——只借
        控制面形态。"""
        for mod in INBOX_MODULES:
            blob = ast.dump(ast.parse(
                (ROUTER / mod).read_text("utf-8"))).lower()
            for word in ("plannotator", "t3code"):
                self.assertNotIn(word, blob, f"{mod} 出现 {word}")


class TestRedLinePins(unittest.TestCase):
    def test_r1_zero_config_write_ast_and_payload_rejected(self):
        """R1 AST+行为：收件箱两模块零网络零子进程；配置写词面零出现；
        approve 携带 closed 词表载荷（config/reload）→
        INBOX_AUTO_EXEC_ATTEMPT 显形拒绝；裁决返回值零配置写/路由切换
        载荷键。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        for mod in INBOX_MODULES:
            imported = _imports_of(ROUTER / mod)
            self.assertFalse(imported & banned,
                             f"{mod} 导入 {imported & banned}")
            names = {n.lower() for n in _code_names(ROUTER / mod)}
            for word in ("write_config", "reload_now", "switch_route"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")
        items = collect(recommendations=RECS, fallback_broken=BROKEN,
                        at=NOW)
        led = InboxLedger()
        led.add(items)
        iid = items[0]["item_id"]
        with self.assertRaisesRegex(InboxError, "INBOX_AUTO_EXEC_ATTEMPT"):
            led.approve(iid, by="operator（占位）", at=NOW,
                        payload={"config": {"route": "b（占位）"}})
        out = led.approve(iid, by="operator（占位）", at=NOW)
        for key in ("config_written", "route_switched", "reloaded"):
            self.assertNotIn(key, out)

    def test_r2_collect_injected_two_sources(self):
        """R2 只读汇聚行为钉值：collect 注入式双源齐收（词表封闭），零
        内核调用面。"""
        items = collect(recommendations=RECS, fallback_broken=BROKEN,
                        at=NOW)
        self.assertEqual([it["kind"] for it in items],
                         ["reroute_recommendation",
                          "fallback_broken_review"])
        self.assertEqual(set(ITEM_KINDS),
                         {"reroute_recommendation",
                          "fallback_broken_review"})

    def test_r3_incomplete_item_refused_pinned(self):
        """R3 行为钉值：推荐项缺 snapshot_digest/recommendation/reason 任一
        → INBOX_ITEM_INCOMPLETE 显形拒绝入箱。"""
        bad = dict(RECS[0])
        del bad["snapshot_digest"]
        with self.assertRaisesRegex(InboxError, "INBOX_ITEM_INCOMPLETE"):
            collect(recommendations=[bad], at=NOW)

    def test_r4_ledger_surface_closed_and_reject_trace(self):
        """R4：InboxLedger 方法面封闭（零删除方法）+reject 留痕钉值。"""
        led = InboxLedger()
        methods = {n for n in dir(led) if not n.startswith("_")}
        self.assertLessEqual(methods, {"add", "approve", "reject", "view",
                                       "events",
                                       "max_recommendation_age_s"}, methods)
        items = collect(recommendations=RECS, at=NOW)
        led.add(items)
        iid = items[0]["item_id"]
        out = led.reject(iid, by="operator（占位）", at=NOW,
                         reason="不换（占位）")
        self.assertEqual(out["reject_reason"], "不换（占位）")
        self.assertEqual(len(led.events), 2)   # add + rejected（只增）


class TestDemarcationPins(unittest.TestCase):
    def test_fallback_gate_surface_unchanged(self):
        """§2 划界钉值：gpu_fallback_gate 面（gpu-fallback-gate/v1 +
        OUTCOME_CODES 词表含 fallback_broken）符号不变（源码 AST 钉值，
        零 import——该模块带 gpu_orchestrator 环境依赖）——回填归 336、
        收件箱零配置写，互不越界。"""
        src = (ROUTER / "gpu_fallback_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        consts = [n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertIn("gpu-fallback-gate/v1", consts, "GATE_SCHEMA 漂移")
        self.assertTrue(any("fallback_broken" in c for c in consts),
                        "OUTCOME_CODES 词表漂移")
        self.assertNotIn("collect_inbox", src,
                         "回填面不应出现收件箱符号")


if __name__ == "__main__":
    unittest.main()
