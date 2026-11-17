"""PRD-483/T2：open-free-router 路由技能评估最小实现测试（PRD-483/T2）。

质量门 + 重叠检测的最小实现测试。断言值来自首份评估报告的真实跑分
（评估脚本按 skill-eval/inventory.json 的真实技能文本算出），不是拍脑袋的数。
"""

from __future__ import annotations

import json
from pathlib import Path

from open_free_router.skill_evaluation import (
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_REMEDIATE,
    evaluate,
    gate_error_rate,
    gate_rate_trio,
    load_inventory,
    render_report,
)

INVENTORY = Path(__file__).resolve().parents[1] / "skill-eval" / "inventory.json"


def test_inventory_loads() -> None:
    inv = load_inventory(INVENTORY)
    assert inv["prd"] == 483
    assert inv["repo"] == "open-free-router"
    assert len(inv["skills"]) == 1
    for skill in inv["skills"]:
        assert skill["id"] and skill["text"] and skill["source_path"]
        assert skill["metrics"]["sample_tasks"] == 0


def test_gate_verdicts_all_insufficient_evidence() -> None:
    report = evaluate(load_inventory(INVENTORY))
    assert report["skill_count"] == 1
    assert all(v["verdict"] == VERDICT_INSUFFICIENT for v in report["verdicts"])
    assert report["remediation_candidates"] == []


def test_overlap_real_numbers() -> None:
    report = evaluate(load_inventory(INVENTORY))
    assert report["pair_count"] == 0
    assert len(report["suspected"]) == 0
    if report["pairs"]:
        top = report["pairs"][0]
        assert top["similarity"] == None
        assert (top["a"], top["b"]) == ("", "")


def test_gate_logic_boundaries() -> None:
    thresholds = {"min_sample_tasks": 30, "max_error_rate": 0.05,
                  "max_error_vs_control_ratio": 0.5, "min_behavior_change_rate": 0.10,
                  "min_adoption_rate": 0.80, "overlap_similarity": 0.8,
                  "error_label": "错误路由率"}
    passing = {"sample_tasks": 30, "error_rate": 0.04, "control_error_rate": 0.10,
               "behavior_change_rate": 0.15, "adoption_rate": 0.85}
    assert gate_rate_trio(passing, thresholds)[0] == VERDICT_PASS
    low_adoption = {"sample_tasks": 30, "error_rate": 0.04, "control_error_rate": 0.10,
                    "behavior_change_rate": 0.15, "adoption_rate": 0.60}
    assert gate_rate_trio(low_adoption, thresholds)[0] == VERDICT_REMEDIATE
    small_sample = {"sample_tasks": 5, "error_rate": 0.04, "control_error_rate": 0.10,
                    "behavior_change_rate": 0.15, "adoption_rate": 0.85}
    assert gate_rate_trio(small_sample, thresholds)[0] == VERDICT_INSUFFICIENT
    no_runs = {"sample_tasks": 0, "error_rate": None, "control_error_rate": None,
               "behavior_change_rate": None, "adoption_rate": None}
    assert gate_rate_trio(no_runs, thresholds)[0] == VERDICT_INSUFFICIENT


def test_report_renders_with_real_numbers() -> None:
    report = evaluate(load_inventory(INVENTORY))
    text = render_report(report)
    assert "2026-11-17" in text
    assert VERDICT_INSUFFICIENT in text
    assert "证据不足不是失效" in text
    assert "0 对" in text
