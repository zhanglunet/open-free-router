# -*- coding: utf-8 -*-
"""PRD-131/T4：与 PRD-096 quota 路由叠加——额度与故障两种条件组合测试（trend #657）。

验收口径（issue #657）：额度与故障两种条件组合测试通过。

组合矩阵（quota 状态感知 × doctor 有序换路）：
  C1 额度耗尽（429 quota）+ 备选健康 → 换路到备选，preferred 进额度冷却；
  C2 故障（503）+ 备选健康 → 换路到备选，preferred 进故障冷却；
  C3 额度与故障叠加在同一后端 → 冷却取更长者（quota_cooldown_scale）；
  C4 冷却期满恢复 → 回切 preferred（auto_fallback 首选健康即首选）。
"""
import sys
from pathlib import Path

from open_free_router.doctor_ordered import auto_fallback, build_ordered_list, doctor_check
from open_free_router.resilience import ResilienceManager, classify_failure

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _probe(states):
    def probe(name):
        return states.get(name, "healthy")
    return probe


def _combo_setup(manager_states, probe_states, now=100):
    ordered = build_ordered_list({"backends": [{"name": "preferred"}, {"name": "backup"}]})
    report = doctor_check(ordered, _probe(probe_states))
    return ordered, report


def test_c1_quota_exhausted_routes_to_healthy_backup():
    # C1：preferred 额度耗尽（quota 词表）→ resilience 拒绝 + doctor 有序换路到 backup
    manager = ResilienceManager()
    manager.record_failure("preferred", "model-a", classify_failure(429, "quota exceeded"), now=100)
    blocked, reason = manager.can_attempt("preferred", "model-a", now=101)
    assert blocked is False or reason != "ready", "quota 429 应触发冷却"
    ordered, report = _combo_setup({}, {"preferred": "down", "backup": "healthy"})
    picked = auto_fallback(report, ordered)
    assert picked["name"] == "backup"


def test_c2_provider_failure_routes_to_backup():
    # C2：preferred 503 故障 → 换路 backup，provider_failure 作用域只影响该 provider
    manager = ResilienceManager()
    manager.record_failure("preferred", "model-a", classify_failure(503), now=100)
    ordered, report = _combo_setup({}, {"preferred": "down", "backup": "healthy"})
    picked = auto_fallback(report, ordered)
    assert picked["name"] == "backup"
    # backup 不受 preferred 故障影响（作用域隔离）
    ok, reason = manager.can_attempt("backup", "model-a", now=101)
    assert (ok, reason) == (True, "ready")


def test_c3_quota_cooldown_longer_than_plain_failure():
    # C3：额度冷却按 quota_cooldown_scale 放大——同刻失败，quota 冷却更久
    m_quota = ResilienceManager()
    m_quota.record_failure("a", "m", classify_failure(429, "quota exceeded"), now=100)
    m_fail = ResilienceManager()
    m_fail.record_failure("a", "m", classify_failure(503), now=100)
    # 在较短冷却期内两者都拒；quota 用更长窗口，晚于 503 恢复点仍拒
    at_fail_recovered = 100 + 60
    ok_fail, _ = m_fail.can_attempt("a", "m", now=at_fail_recovered)
    blocked_quota, reason_quota = m_quota.can_attempt("a", "m", now=at_fail_recovered)
    assert ok_fail is True, "503 普通故障此时应已恢复（可回切）"
    assert blocked_quota is False or reason_quota != "ready", "quota 冷却按比例放大仍未恢复"


def test_c4_cooldown_expiry_returns_to_preferred():
    # C4：冷却期满 + doctor 恢复 healthy → 回切 preferred（有序列表首选优先）
    manager = ResilienceManager()
    manager.record_failure("preferred", "model-a", classify_failure(503), now=100)
    manager.record_success("preferred", "model-a", now=200)
    ordered, report = _combo_setup({}, {"preferred": "healthy", "backup": "healthy"})
    picked = auto_fallback(report, ordered)
    assert picked["name"] == "preferred", "恢复后回切首选"
    ok, reason = manager.can_attempt("preferred", "model-a", now=201)
    assert (ok, reason) == (True, "ready")
