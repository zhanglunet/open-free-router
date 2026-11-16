# -*- coding: utf-8 -*-
"""PRD-467/T2 测试（trend #2256 的 T2 环节）：按密钥额度跟踪。
运行：.venv/bin/python -m pytest tests/test_prd467_t2_quota_ledger.py -q
覆盖（T1 §2/§3）：四维滚动窗口+键格式隐私（无密钥原文）/在途租约
（进行中也算数）/严格小于守门/上限三档优先级+过期降档/丢计数保守
（quota_counter_lost）/reset_at 不复位（不实现清零）/池守门/多退少补
只退不追。零网络；全合成占位。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.quota_ledger import (  # noqa: E402
    COMPLETION_RESERVE_DEFAULT, ERROR_CODES, QUOTA_COUNTER_LOST, QuotaLedger,
    QuotaLedgerError, WINDOW_SECONDS, count_key, pool_key,
)

T0 = 1000000.0     # 固定注入时钟起点（占位）


def mk(**kw):
    return QuotaLedger(now=T0, **kw)


class TestWindows(unittest.TestCase):
    def test_rpm_window_rolls(self):
        led = mk()
        led.record_dispatch(platform="deepseek", model_id="chat", slot=0)
        ok, _ = led.can_dispatch(platform="deepseek", model_id="chat", slot=0)
        self.assertTrue(ok)                      # 无上限：只计数不守门
        led.advance(61)                          # 滚出窗口
        r, _ = led.windows[count_key("deepseek", "chat", 0, "rpm")].count(
            led.now)
        self.assertEqual(r, 0)                   # 60 秒前的桶已过期

    def test_key_format_no_secret_material(self):
        k = count_key("deepseek", "chat", 3, "rpm")
        self.assertEqual(k, "deepseek:chat:slot-3:rpm")
        self.assertNotIn("sk-", k)

    def test_pool_key(self):
        self.assertEqual(pool_key("openrouter-free（占位）", "rpd"),
                         "pool:openrouter-free（占位）:rpd")


class TestLeaseGate(unittest.TestCase):
    def test_lease_counts_as_in_flight(self):
        """§3.1 并行请求不越线：领了租约就算数（进行中也算数）。"""
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 2, source="documented")
        led.acquire_lease(platform="groq", model_id="llama", slot=0)
        allowed, _ = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertTrue(allowed)                 # 1 在途 < 2
        led.acquire_lease(platform="groq", model_id="llama", slot=0)
        allowed, reason = led.can_dispatch(platform="groq", model_id="llama",
                                           slot=0)
        self.assertFalse(allowed)                # 2 在途，再派就到上限
        self.assertEqual(reason, "QUOTA_EXHAUSTED")

    def test_strict_less_than(self):
        """到上限就停：recorded+in_flight == limit 也不许再发。"""
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 1, source="documented")
        led.acquire_lease(platform="groq", model_id="llama", slot=0)
        allowed, reason = led.can_dispatch(platform="groq", model_id="llama",
                                           slot=0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "QUOTA_EXHAUSTED")

    def test_release_settles_count(self):
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 2, source="documented")
        lease = led.acquire_lease(platform="groq", model_id="llama", slot=0)
        led.release_lease(lease_id=lease["lease_id"], actual_tokens=100)
        allowed, _ = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertTrue(allowed)                 # 落定 1 < 2

    def test_completion_reserve_default_512(self):
        self.assertEqual(COMPLETION_RESERVE_DEFAULT, 512)
        led = mk()
        led.set_limit("groq", "llama", 0, "tpm", 600, source="documented")
        lease = led.acquire_lease(platform="groq", model_id="llama", slot=0)
        self.assertEqual(lease["reserved_tokens"], 512)   # 未给 est → 占预留

    def test_under_reserve_observed_only(self):
        """只退不追：实际超预留不补扣，记 tokens_under_reserved 观测。"""
        led = mk()
        lease = led.acquire_lease(platform="groq", model_id="llama", slot=0,
                                  est_tokens=100)
        led.release_lease(lease_id=lease["lease_id"], actual_tokens=300)
        self.assertEqual(led.observations_under_reserve, 1)


class TestLimitSources(unittest.TestCase):
    def test_observed_header_expires_after_30min(self):
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 10, source="observed_header",
                      observed_at=T0)
        ok, _ = led.can_dispatch(platform="groq", model_id="llama", slot=0)
        self.assertTrue(ok)
        led.advance(1801)                        # 超过 30 分钟 → 过期降档
        lim = led.get_limit("groq", "llama", 0, "rpm")
        self.assertIsNone(lim)                   # 降级到下一档（无 → 不守门）

    def test_observed_freshest_wins(self):
        """§2.3 优先级：上游头观测（最新鲜）> 文档 > 目录——设计写死。"""
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
        led.set_limit("groq", "llama", 0, "rpm", 999, source="observed_header",
                      observed_at=T0)
        lim = led.get_limit("groq", "llama", 0, "rpm")
        self.assertEqual((lim["limit"], lim["source"]), (999, "observed_header"))

    def test_no_limit_counts_only(self):
        """三档全无：只计数不守门——没有证据不定罪。"""
        led = mk()
        for _ in range(50):
            led.record_dispatch(platform="groq", model_id="llama", slot=0)
        ok, _ = led.can_dispatch(platform="groq", model_id="llama", slot=0)
        self.assertTrue(ok)                      # 只计数，不拦


class TestCounterLost(unittest.TestCase):
    def test_counter_lost_means_exhausted(self):
        """§2.5 安全不变量：上限已知、已用未知 → 按已耗尽（不静默放行）。"""
        led = mk()
        led.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
        led.mark_counter_lost("groq", "llama", 0)
        allowed, reason = led.can_dispatch(platform="groq", model_id="llama",
                                           slot=0)
        self.assertFalse(allowed)
        self.assertEqual(reason, QUOTA_COUNTER_LOST)

    def test_counter_lost_without_limit_counts_only(self):
        """上限未知 + 已用未知：仍只计数（拦就是编数字）。"""
        led = mk()
        led.mark_counter_lost("groq", "llama", 0)
        ok, _ = led.can_dispatch(platform="groq", model_id="llama", slot=0)
        self.assertTrue(ok)


class TestResetAtNotReset(unittest.TestCase):
    def test_window_does_not_reset_on_reset_at(self):
        """§2.6：reset_at 只是提示——不得实现到点清零。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "quota_ledger.py").read_text(encoding="utf-8")
        for banned in ("reset_at 清零", "clear_counter", "reset_window"):
            self.assertNotIn(banned, src, banned)
        # 窗口滚出才算过期（61 秒后 rpm 清零是窗口滚动，不是 reset_at 复位）


class TestPoolGate(unittest.TestCase):
    def test_pool_gate(self):
        """§5 共享池按池计量（OpenRouter :free 多模型共用一池）。"""
        led = mk()
        led.set_limit("pool", "openrouter-free（占位）", 0, "rpm", 2,
                      source="documented")
        led.record_pool_dispatch(pool_id="openrouter-free（占位）")
        led.record_pool_dispatch(pool_id="openrouter-free（占位）")
        ok, reason = led.can_dispatch_pool(pool_id="openrouter-free（占位）",
                                           limit=2)
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTA_EXHAUSTED")


class TestRedline(unittest.TestCase):
    def test_key_no_secret_material_ast(self):
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "quota_ledger.py").read_text(encoding="utf-8")
        self.assertNotIn("api_key", src)
        self.assertNotIn("decrypt", src)

    def test_no_reset_at_clear_logic(self):
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "quota_ledger.py").read_text(encoding="utf-8")
        self.assertNotIn("reset_at", src)        # §2.6：窗口不复位

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"QUOTA_INCOMPLETE", "QUOTA_COUNTER_LOST",
                          "QUOTA_EXHAUSTED"})
        self.assertEqual(set(WINDOW_SECONDS),
                         {"rpm", "rpd", "tpm", "tpd"})

    def test_ast_zero_network(self):
        import ast
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "quota_ledger.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"urllib", "socket", "http"},
                         imported & {"urllib", "socket", "http"})


if __name__ == "__main__":
    unittest.main()
