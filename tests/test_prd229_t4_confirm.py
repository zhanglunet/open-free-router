# -*- coding: utf-8 -*-
"""PRD-229/T4 测试（trend #1148）：人工确认与应用流程。

覆盖（T1 §6）：状态机（pending→confirmed→applied→rolled_back/closed、
rejected/stale/expired 终态、非法迁移拒）、三重不生效机制（存储隔离 0600/
0700、应用器单闸门四项校验——非 confirmed 拒/无确认记录拒/证据哈希不一致
自动置 stale/过期置 stale 拒、显式确认 operator 必填）、append-only 审计
流水字段齐全（generated/confirmed/rejected/applied/rolled_back/stale）、
回滚旧值快照与二次应用拒绝、确定性。零网络；全占位。
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "open_free_router"))

from learning_confirm import (  # noqa: E402
    ConfirmError,
    LearningConfirmBoard,
    evidence_hash,
    save_journal,
)

AT0 = "2026-11-01T09:00:00Z"
AT1 = "2026-11-02T09:00:00Z"
AT2 = "2026-11-03T09:00:00Z"
AT3 = "2026-11-12T09:00:00Z"  # 过 expires_at（+7d=11-08）之后


def _record(**overrides):
    record = {
        "recommendation_id": "rec-abc12345",
        "kind": "recommendation", "generated_at": AT0, "status": "pending",
        "scope": {"prompt_class": "remote-api", "virtual_model": None},
        "parameter": "candidate_first",
        "current_value": {"provider": "remote", "model": "model-a"},
        "recommended_value": {"provider": "remote", "model": "model-b"},
        "evidence": {"window": {"from": "2026-10-24", "to": "2026-11-08"},
                     "groups": [{"provider": "remote", "model": "model-a"},
                                {"provider": "remote", "model": "model-b"}],
                     "rules_passed": ["R1", "R2"]},
        "expected_benefit": {"metric": "ttfb_p95_ms"},
        "expires_at": "2026-11-08T09:00:00Z",
    }
    record.update(overrides)
    return record


class TestStateMachine:
    def test_full_happy_path(self):
        board = LearningConfirmBoard()
        board.register(_record())
        board.confirm("rec-abc12345", "operator-01", at=AT1)
        board.apply("rec-abc12345", at=AT2)
        board.rollback("rec-abc12345", "operator-01", at=AT3)
        board.close("rec-abc12345", at=AT3)
        assert board.recommendations["rec-abc12345"]["status"] == "closed"

    def test_reject_path(self):
        board = LearningConfirmBoard()
        board.register(_record())
        board.reject("rec-abc12345", "operator-01", at=AT1, reason="样本存疑")
        assert board.recommendations["rec-abc12345"]["status"] == "rejected"
        with pytest.raises(ConfirmError):
            board.confirm("rec-abc12345", "operator-01", at=AT2)

    def test_illegal_transition_rejected(self):
        board = LearningConfirmBoard()
        board.register(_record())
        with pytest.raises(ConfirmError, match="非法迁移"):
            board.apply("rec-abc12345", at=AT1)  # pending 直接 apply

    def test_missing_recommendation(self):
        board = LearningConfirmBoard()
        with pytest.raises(ConfirmError, match="不存在"):
            board.confirm("ghost", "op", at=AT1)

    def test_register_requires_pending(self):
        board = LearningConfirmBoard()
        rec = _record(status="confirmed")
        with pytest.raises(ConfirmError, match="pending"):
            board.register(rec)


class TestTripleGate:
    def test_confirm_requires_operator(self):
        board = LearningConfirmBoard()
        board.register(_record())
        with pytest.raises(ConfirmError, match="operator 必填"):
            board.confirm("rec-abc12345", "", at=AT1)

    def test_apply_requires_confirmed_status(self):
        board = LearningConfirmBoard()
        board.register(_record())
        with pytest.raises(ConfirmError, match="非法迁移"):
            board.apply("rec-abc12345", at=AT1)

    def test_apply_without_confirm_record_rejected(self):
        """② 单闸门：无确认记录（确认是显式动作）→ 拒绝。"""
        board = LearningConfirmBoard()
        board.register(_record())
        rec = board.recommendations["rec-abc12345"]
        rec["status"] = "confirmed"  # 越过显式 confirm 直改状态（无确认记录）
        with pytest.raises(ConfirmError, match="无确认记录"):
            board.apply("rec-abc12345", at=AT1)

    def test_evidence_tamper_autostales(self):
        """② 证据哈希不一致 → 自动置 stale，不得应用。"""
        board = LearningConfirmBoard()
        board.register(_record())
        board.confirm("rec-abc12345", "operator-01", at=AT1)
        board.recommendations["rec-abc12345"]["evidence"]["groups"][0]["n"] = 99999
        with pytest.raises(ConfirmError, match="stale"):
            board.apply("rec-abc12345", at=AT2)
        assert board.recommendations["rec-abc12345"]["status"] == "stale"

    def test_expired_apply_stales_and_rejects(self):
        """④ 过期 → 置 stale，不得应用。"""
        board = LearningConfirmBoard()
        board.register(_record())
        board.confirm("rec-abc12345", "operator-01", at=AT1)
        with pytest.raises(ConfirmError, match="过期"):
            board.apply("rec-abc12345", at=AT3)
        assert board.recommendations["rec-abc12345"]["status"] == "stale"


class TestAuditAndStorage:
    def test_audit_journal_append_only_full_fields(self):
        """§6.3：generated/confirmed/rejected 事件字段齐全（rejected 走独立板，
        因 confirmed→rejected 非法）。"""
        board = LearningConfirmBoard()
        board.register(_record())
        board.confirm("rec-abc12345", "operator-01", at=AT1)
        confirmed = board.journal[1]
        assert confirmed["operator"] == "operator-01"
        assert confirmed["evidence_hash"] == evidence_hash(_record()["evidence"])

        board2 = LearningConfirmBoard()
        board2.register(_record())
        board2.reject("rec-abc12345", "operator-01", at=AT1, reason="拒因（占位）")
        rejected = board2.journal[1]
        assert rejected["type"] == "rejected"
        assert rejected["reason"] == "拒因（占位）"
        types = [e["type"] for e in board2.journal]
        assert types == ["generated", "rejected"]

    def test_rollback_uses_old_value_snapshot_and_blocks_second_apply(self):
        board = LearningConfirmBoard()
        board.register(_record())
        board.confirm("rec-abc12345", "operator-01", at=AT1)
        board.apply("rec-abc12345", at=AT2)
        board.rollback("rec-abc12345", "operator-01", at=AT3)
        rec = board.recommendations["rec-abc12345"]
        assert rec["old_values_snapshot"] == {"provider": "remote", "model": "model-a"}
        with pytest.raises(ConfirmError, match="非法迁移"):
            board.apply("rec-abc12345", at=AT3)  # 二次应用拒绝

    def test_save_journal_permissions(self):
        board = LearningConfirmBoard()
        board.register(_record())
        with tempfile.TemporaryDirectory() as tmp:
            path = save_journal(board, tmp)
            assert stat.S_IMODE(os.stat(path).st_mode) & 0o777 == 0o600
            assert stat.S_IMODE(os.stat(tmp).st_mode) & 0o777 == 0o700
            lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
            assert json.loads(lines[0])["type"] == "generated"


if __name__ == "__main__":
    pytest.main([__file__])

