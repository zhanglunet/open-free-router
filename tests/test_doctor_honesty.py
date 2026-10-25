"""PRD-174/T2: doctor 每条结论带诚实标注（T1 §2.3/§3/§6）。

T1 契约（``docs/plans/2026-10-25-doctor-honest-labeling.md``，trend issue #870）：
§3.1 检查项形状扩展 + honesty_summary、§3.2 检查组 × 标注矩阵（唯一口径）、
§6 反模式红线（绝不默认 extracted、推导恒推断、过期证据存疑）。
样例输出必须可解析（doctor --json | 解析 honesty 全量通过）。
"""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_free_router.cli import cmd_doctor
from open_free_router.failover_audit import (
    HONESTY_LABELS, HONESTY_TAGS_ZH, honesty_summary_from_report,
)

CONCLUSION_GROUPS = ("checks", "routing", "free_tier", "protocol_matrix")


def _doctor_env(tmp_path, monkeypatch, registry: str, routing: str = "",
                reachable: bool = True):
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\n"
        f"data_dir: {tmp_path / 'data'}\n"
        + routing
    )
    (tmp_path / "registry.yaml").write_text(registry)
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    monkeypatch.setattr(
        "open_free_router.cli._status_payload",
        lambda cfg: {
            "providers": 1, "providers_with_key": 1, "models": 1,
            "proxy_reachable": reachable, "ui_reachable": reachable,
            "proxy_url": "http://127.0.0.1:8337",
            "ui_url": "http://127.0.0.1:9057",
        },
    )
    return tmp_path


HEALTHY_REGISTRY = (
    "provider-a:\n"
    "  base_url: https://<provider-a-host>/v1\n"
    "  api_key: test-placeholder\n"
    "  models:\n"
    "    - id: model-x\n"
)

# --------------------------------------------- T1 §6 anchor 1: no defaults ---

def _iter_function_defaults(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]
            for default in defaults:
                yield node, default


def test_no_function_parameter_defaults_to_extracted():
    # T1 §2.3 规则 1 / §6 锚点 1：不存在「想不起来就标 extracted」的分支；
    # extracted 绝不出现在任何函数参数默认值里。
    src_root = Path(__file__).resolve().parents[1] / "src" / "open_free_router"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node, default in _iter_function_defaults(tree):
            if isinstance(default, ast.Constant) and default.value == "extracted":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_honesty_vocabulary_is_exactly_the_t1_enum():
    assert HONESTY_LABELS == frozenset({"extracted", "inferred", "ambiguous"})
    assert set(HONESTY_TAGS_ZH) == set(HONESTY_LABELS)
    assert HONESTY_TAGS_ZH == {"extracted": "实测", "inferred": "推断", "ambiguous": "存疑"}


# ------------------------------------------ T1 §3.1: every entry labeled ----

def _run_doctor_json(tmp_path, monkeypatch, capsys, registry=HEALTHY_REGISTRY,
                     routing="", reachable=True):
    _doctor_env(tmp_path, monkeypatch, registry, routing=routing, reachable=reachable)
    try:
        cmd_doctor(SimpleNamespace(json=True))
        exit_code = 0  # 健康路径直接返回，不抛 SystemExit
    except SystemExit as stopped:
        exit_code = stopped.code
    report = json.loads(capsys.readouterr().out)
    return report, exit_code


def test_doctor_json_labels_every_conclusion_entry(tmp_path, monkeypatch, capsys):
    report, exit_code = _run_doctor_json(tmp_path, monkeypatch, capsys)
    assert exit_code == 0
    for group in CONCLUSION_GROUPS:
        items = report[group]
        if group in ("free_tier", "protocol_matrix"):
            items = items["issues"]
        for item in items:
            # 缺失 honesty 视实现 bug（T1 §3.1）。
            assert item["honesty"] in HONESTY_LABELS, (group, item)
    assert report["checks"], "doctor must always run its own checks"


def test_doctor_json_is_parseable_and_summary_counts_match(tmp_path, monkeypatch, capsys):
    report, _ = _run_doctor_json(tmp_path, monkeypatch, capsys)
    # 样例输出可解析（T2 验收）：stdout 即合法 JSON，可二次序列化。
    assert json.loads(json.dumps(report))["ok"] is True
    summary = report["honesty_summary"]
    assert set(summary) == {"extracted", "inferred", "ambiguous"}
    counted = 0
    for group in CONCLUSION_GROUPS:
        items = report[group]
        if group in ("free_tier", "protocol_matrix"):
            items = items["issues"]
        counted += sum(1 for item in items if item.get("honesty") in HONESTY_LABELS)
    assert sum(summary.values()) == counted
    # 健康路径：所有 checks 均为本轮真实读取（T1 §3.2 rows 1-2）；
    # 推断项恰好是路由规则推导出的告警（默认 auto 别名在最小注册表上）。
    assert summary["extracted"] == len(report["checks"])
    assert summary["inferred"] == len(report["routing"])


def test_human_output_appends_honesty_tags(tmp_path, monkeypatch, capsys):
    _doctor_env(tmp_path, monkeypatch, HEALTHY_REGISTRY)
    cmd_doctor(SimpleNamespace())  # 人读模式
    out = capsys.readouterr().out
    # 带 detail 的行：标签追加在行尾（T1 §3.1 人读样例）。
    assert re.search(r"^  ✓ registry.yaml found — \S+ \[实测\]$", out, re.M)
    assert re.search(r"^  ✓ declared upstream protocol configuration is valid \[推断\]$",
                     out, re.M)
    # 健康最小注册表上，默认 auto 别名产生路由告警：推导结论标推断。
    assert re.search(r"^  ⚠ routing\.aliases\.auto/coding: .* \[推断\]$", out, re.M)
    # 客户端存在性是提示性实测条目。
    assert re.search(r"^  [·✓] Codex profile\s+\S+ \[实测\]$", out, re.M)


# --------------------------------- T1 §3.2 matrix rows 3-8: derived = inferred

def test_protocol_and_routing_issues_are_always_inferred(tmp_path, monkeypatch, capsys):
    registry = (
        # 空端点 → 协议矩阵 invalid_upstream_url（声明推导，不是实测）。
        "provider-a:\n"
        "  api_key: test-placeholder\n"
        "  models:\n"
        "    - id: model-x\n"
    )
    routing = (
        "routing:\n"
        "  aliases:\n"
        "    preferred:\n"
        "      candidates: [missing]\n"
    )
    report, exit_code = _run_doctor_json(
        tmp_path, monkeypatch, capsys, registry=registry, routing=routing)
    assert exit_code == 1
    assert report["protocol_matrix"]["issues"], "expected a declared-config issue"
    for issue in report["protocol_matrix"]["issues"]:
        assert issue["honesty"] == "inferred"
    codes = {issue["code"] for issue in report["routing"]}
    assert "candidate_not_registered" in codes
    for issue in report["routing"]:
        assert issue["honesty"] == "inferred"
    # 顶层汇总是 severity 计数的确定性推导（T1 §3.2 row 8）：恒 inferred。
    assert report["honesty"] == "inferred"
    assert report["honesty_summary"]["inferred"] >= 2


# ------------------------------------- T1 §6 anchor 2: no recycled probing ---

def test_unreachable_proxy_is_ambiguous_never_recycled(tmp_path, monkeypatch, capsys):
    report, _ = _run_doctor_json(
        tmp_path, monkeypatch, capsys, reachable=False)
    by_label = {item["label"]: item for item in report["checks"]}
    for label in ("proxy reachable", "dashboard reachable"):
        # 依赖进程不在 = 本轮没有上游观测，标存疑，不沿用上次结果冒充。
        assert by_label[label]["honesty"] == "ambiguous", label
        assert "上次" not in by_label[label]["detail"]
    # 其余文件/注册表读取仍是本轮实测。
    assert by_label["registry.yaml found"]["honesty"] == "extracted"


# ------------------------------------- T1 §6 anchors 3-4: evidence honesty ---

def test_expired_evidence_is_ambiguous_and_invalid_is_extracted(tmp_path, monkeypatch, capsys):
    registry = (
        "provider-a:\n"
        "  base_url: https://<provider-a-host>/v1\n"
        "  api_key: test-placeholder\n"
        "  models:\n"
        "    - id: model-expired\n"
        "      free_tier:\n"
        "        type: unlimited_claimed\n"
        "        evidence_url: https://example.invalid/terms\n"
        "        verified_at: \"2026-01-01T00:00:00+00:00\"\n"
        "        expires_at: \"2026-02-01T00:00:00+00:00\"\n"
        "    - id: model-invalid\n"
        "      free_tier:\n"
        "        type: not-a-real-type\n"
    )
    report, exit_code = _run_doctor_json(tmp_path, monkeypatch, capsys, registry=registry)
    assert exit_code == 1
    issues = {issue["code"]: issue for issue in report["free_tier"]["issues"]}
    assert issues["free_tier_expired"]["honesty"] == "ambiguous"
    assert issues["invalid_free_tier_evidence"]["honesty"] == "extracted"


# --------------------------------------------- summary reducer edge cases ---

def test_honesty_summary_ignores_clients_and_unlabeled_entries():
    report = {
        "checks": [{"severity": "ok", "honesty": "extracted"}],
        "routing": [{"severity": "error"}],  # 旧数据无标注：不回填、不计数
        "free_tier": {"issues": [{"severity": "warning", "honesty": "ambiguous"}]},
        "protocol_matrix": {"issues": [{"severity": "warning", "honesty": "bogus"}]},
        "clients": [{"name": "Codex profile", "honesty": "extracted"}],  # 提示性条目不入计数
    }
    assert honesty_summary_from_report(report) == {
        "extracted": 1, "inferred": 0, "ambiguous": 1,
    }
    assert honesty_summary_from_report(None) == {
        "extracted": 0, "inferred": 0, "ambiguous": 0,
    }
