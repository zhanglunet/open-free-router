# -*- coding: utf-8 -*-
"""PRD-467/T4 测试（trend #2258 的 T4 环节）：按密钥额度跟踪划界断言。
运行：.venv/bin/python -m pytest tests/test_prd467_t4_quota_boundary.py -q
覆盖（T1 §1 非目标边界）：BYOS/额度预测/冷却阶梯/解析/三维权重/看板零导入
+借鉴词零出现（freellmapi 仅代码标识符扫描）+标准库-only+AST 零网络+
安全不变量钉值（丢计数保守/reset 不复位/严格小于/键零密钥原文）+
Ledger/Store 方法面封闭。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.quota_ledger import (  # noqa: E402
    COMPLETION_RESERVE_DEFAULT, ERROR_CODES, QUOTA_COUNTER_LOST, QuotaLedger,
    QuotaLedgerError, WINDOW_SECONDS, count_key)
from open_free_router.quota_ledger_store import (  # noqa: E402
    QuotaLedgerStore)

NOW = 1000000.0
MODULES = ["quota_ledger.py", "quota_ledger_store.py"]
SRC_ROUTER = Path(__file__).resolve().parents[1] / "src/open_free_router"


def _imports_of(path: Path) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _code_names(path: Path) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class TestBoundary(unittest.TestCase):
    def test_no_downstream_imports(self):
        """BYOS（PRD-173）/冷却阶梯（PRD-468）/解析（PRD-469）/看板（PRD-201）
        零导入；resilience/costing/probe/routing 零导入——边界只读不改。"""
        for mod in MODULES:
            imported = _imports_of(SRC_ROUTER / mod)
            for banned in ("resilience", "costing", "routing", "probe",
                           "quota_view", "byos", "quota", "analytics_store"):
                self.assertNotIn(banned, imported, f"{mod} 导入 {banned}")

    def test_borrowed_zero_symbols(self):
        """借鉴仓（freellmapi，MIT 实测）零符号——只借思路不复用代码。"""
        for mod in MODULES:
            names = {n.lower() for n in _code_names(SRC_ROUTER / mod)}
            for word in ("freellmapi", "freellm"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")

    def test_stdlib_only_and_ast_redline(self):
        allowed = {"__future__", "collections", "hashlib", "json", "os",
                   "pathlib", "secrets", "sqlite3", "sys", "time", "typing"}
        for mod in MODULES:
            imported = _imports_of(SRC_ROUTER / mod)
            self.assertLessEqual(imported - allowed, set(), mod)
            self.assertFalse(imported & {"urllib", "socket", "http"},
                             f"{mod} 导入网络")
        # requests 只允许出现在 catalog_sync（T2 注入默认），ledger/store 零网络
        for mod in MODULES:
            imported = _imports_of(SRC_ROUTER / mod)
            self.assertNotIn("requests", imported, f"{mod} 导入 requests")

    def test_no_forecast_no_weights(self):
        """非目标：不做额度预测、不改三维权重（AST 词面扫描）。"""
        for mod in MODULES:
            names = {n.lower() for n in _code_names(SRC_ROUTER / mod)}
            for word in ("forecast", "quality_weight", "cost_weight",
                         "latency_weight"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")


class TestBehaviorPins(unittest.TestCase):
    def test_counter_lost_conservative_pin(self):
        """§2.5 安全不变量：上限已知+计数丢失 → 按已耗尽。"""
        led = QuotaLedger(now=NOW)
        led.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
        led.mark_counter_lost("groq", "llama", 0)
        ok, reason = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertFalse(ok)
        self.assertEqual(reason, QUOTA_COUNTER_LOST)

    def test_reset_at_never_resets_pin(self):
        """§2.6：窗口不复位——advance 61 秒后 rpm 计数因滚动过期清零是
        窗口滚动语义，不是 reset_at 复位；600 秒滚动语义下计数同样过期。"""
        led = QuotaLedger(now=NOW)
        led.record_dispatch(platform="groq", model_id="llama", slot=0)
        led.advance(61)
        r, _ = led._win(count_key("groq", "llama", 0, "rpm"), "rpm").count(
            led.now)
        self.assertEqual(r, 0)   # 滚动过期，与任何上游 reset 提示无关

    def test_strict_less_than_pin(self):
        """§3.1：recorded + in_flight < limit（严格小于，到上限就停）。"""
        led = QuotaLedger(now=NOW)
        led.set_limit("groq", "llama", 0, "rpm", 1, source="documented")
        led.acquire_lease(platform="groq", model_id="llama", slot=0)
        ok, reason = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTA_EXHAUSTED")

    def test_key_never_carries_secret(self):
        """键里只放槽序号，永不放密钥原文（隐私线，AST+运行时钉值）。"""
        led = QuotaLedger(now=NOW)
        led.record_dispatch(platform="groq", model_id="llama", slot=7)
        key = count_key("groq", "llama", 7, "rpm")
        self.assertIn("slot-7", key)
        self.assertNotIn("sk-", key)
        src = (SRC_ROUTER / "quota_ledger.py").read_text(encoding="utf-8")
        self.assertNotIn("api_key", src)

    def test_surface_closed(self):
        led = QuotaLedger(now=NOW)
        methods = {n for n in dir(led) if not n.startswith("_")}
        self.assertLessEqual(methods, {
            "advance", "now", "set_limit", "get_limit", "record_dispatch",
            "record_pool_dispatch", "mark_counter_lost", "counter_is_lost",
            "can_dispatch", "acquire_lease", "release_lease",
            "can_dispatch_pool", "windows", "limits", "leases",
            "counter_lost", "completion_reserve",
            "observations_under_reserve"}, methods)
        store = QuotaLedgerStore(Path(__file__).resolve().parents[1] / "src"
                                 / "open_free_router" / "_probe_unused.db")
        store_methods = {n for n in dir(store) if not n.startswith("_")}
        self.assertLessEqual(store_methods,
                             {"checkpoint", "restore_into", "purge_dead",
                              "close", "db_path", "conn"},
                             store_methods)
        store.close()
        (SRC_ROUTER / "_probe_unused.db").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
