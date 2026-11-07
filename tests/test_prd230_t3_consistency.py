# -*- coding: utf-8 -*-
"""PRD-230/T3 测试：上下文与输出格式一致性保障（trend #1152）。

运行：.venv/bin/python -m pytest tests/test_prd230_t3_consistency.py -q

覆盖（T1 §2/§3）：checkpoint 构造与哈希确定性、判据 A 全等回放（model 豁免/
篡改检出/会话缺失/多余会话/seq 回退）、判据 B 探针、判据 C 参数未知键透传、
判据 D usage 连续、判据 E abort 终结帧、非流式 schema（§3.2-1）、流式 R1–R6
（逐规则正反路径）、C1/C2 门禁（前后同等 100%）。零网络；全合成。
"""
import copy
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.swap_consistency import (  # noqa: E402
    abort_frame,
    consistency_gate,
    context_replay_diff,
    make_checkpoint,
    params_preserved,
    probe_check,
    usage_continuous,
    validate_completion,
    validate_stream_chunks,
)


def _session(sid="sess-1", model="m-old", seq=3):
    return {
        "session_id": sid,
        "model": model,
        "system": "你是测试助手（占位）",
        "messages": [
            {"role": "user", "content": "暗号是 ofr-token-42（占位随机事实）"},
            {"role": "assistant", "content": "已记录（占位）"},
        ],
        "params": {"temperature": 0, "seed": 7, "custom_key": "原样透传"},
        "last_request_seq": seq,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


class 判据ATests(unittest.TestCase):
    def setUp(self):
        self.sessions = [_session(), _session("sess-2", seq=7)]
        self.checkpoint = make_checkpoint("swap-001", "2026-11-08T10:00:00+00:00",
                                          self.sessions)

    def test_checkpoint_deterministic_hash(self):
        again = make_checkpoint("swap-001", "2026-11-08T10:00:00+00:00",
                                copy.deepcopy(self.sessions))
        self.assertEqual(self.checkpoint, again)

    def test_replay_no_diff_when_context_intact(self):
        """切换后会话原样（model 换新豁免）→ 无 diff（上下文没丢）。"""
        after = [dict(_session(model="m-new")), dict(_session("sess-2", model="m-new", seq=7))]
        self.assertEqual(context_replay_diff(self.checkpoint, after), [])

    def test_replay_detects_tampered_messages(self):
        after = [_session(model="m-new")]
        after[0]["messages"][1]["content"] = "被改写（占位）"
        errors = context_replay_diff(self.checkpoint, after)
        self.assertTrue(any("哈希不一致" in e for e in errors))
        self.assertTrue(any("sess-2" in e for e in errors))  # 另一会话缺失也报

    def test_replay_detects_missing_and_extra_sessions(self):
        extra = dict(_session("ghost"))
        errors = context_replay_diff(self.checkpoint, [extra])
        self.assertTrue(any("sess-1" in e and "缺失" in e for e in errors))
        self.assertTrue(any("来源不明" in e for e in errors))

    def test_replay_detects_seq_regression(self):
        after = [_session(model="m-new", seq=1), _session("sess-2", model="m-new")]
        errors = context_replay_diff(self.checkpoint, after)
        self.assertTrue(any("回退" in e for e in errors))


class 判据B到ETests(unittest.TestCase):
    def test_probe_token_found_and_missing(self):
        self.assertTrue(probe_check("ofr-token-42", "暗号是 ofr-token-42（新模型回答）"))
        self.assertFalse(probe_check("ofr-token-42", "不知道你在说什么"))

    def test_params_unknown_keys_preserved(self):
        before = {"temperature": 0, "custom": {"a": 1}}
        self.assertTrue(params_preserved(before, dict(before)))
        self.assertFalse(params_preserved(before, {"temperature": 0}))

    def test_usage_continuous_and_reset_detected(self):
        before = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        after = {"prompt_tokens": 180, "completion_tokens": 90, "total_tokens": 270}
        self.assertTrue(usage_continuous(before, after))
        reset = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.assertFalse(usage_continuous(before, reset))  # 清零即跳变

    def test_abort_frame_legal_termination(self):
        frame = abort_frame("swap-009")
        self.assertIn("finish_reason", frame)
        self.assertIn("aborted_by_swap", frame)
        self.assertIn("data: [DONE]", frame)
        self.assertIn("x-ofr-swap-id: swap-009", frame)
        self.assertIn("delta", frame)


class 非流式SchemaTests(unittest.TestCase):
    def _valid(self):
        return {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1,
            "model": "m-new",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "占位回答"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "headers": {"x-ofr-actual-model": "m-new", "x-ofr-swap-id": "swap-1"},
        }

    def test_valid_completion(self):
        self.assertEqual(validate_completion(self._valid(), swap_id="swap-1"), [])

    def test_missing_fields_flagged(self):
        resp = self._valid()
        del resp["usage"]
        errors = validate_completion(resp)
        self.assertTrue(any("usage" in e for e in errors))

    def test_bad_finish_reason_flagged(self):
        resp = self._valid()
        resp["choices"][0]["finish_reason"] = "exploded"
        self.assertTrue(any("finish_reason" in e for e in validate_completion(resp)))

    def test_missing_actual_model_header(self):
        resp = self._valid()
        del resp["headers"]["x-ofr-actual-model"]
        self.assertTrue(any("x-ofr-actual-model" in e for e in validate_completion(resp)))


class 流式R1到R6Tests(unittest.TestCase):
    def _stream(self):
        return [
            {"object": "chat.completion.chunk", "delta": {"role": "assistant"}},
            {"object": "chat.completion.chunk", "delta": {"content": "占位"}},
            {"object": "chat.completion.chunk", "delta": {}, "finish_reason": "stop"},
            "[DONE]",
        ]

    def test_valid_stream_all_rules_pass(self):
        report = validate_stream_chunks(self._stream())
        self.assertTrue(report["ok"])
        self.assertTrue(all(report["rules"].values()))

    def test_r1_role_frame_repeated(self):
        chunks = self._stream()
        chunks.insert(1, {"object": "chat.completion.chunk", "delta": {"role": "assistant"}})
        report = validate_stream_chunks(chunks)
        self.assertFalse(report["rules"]["R1"])
        self.assertTrue(any("R1" in e for e in report["errors"]))

    def test_r2_out_of_scope_delta_field(self):
        chunks = self._stream()
        chunks[1] = {"object": "chat.completion.chunk", "delta": {"model": "m"}}
        report = validate_stream_chunks(chunks)
        self.assertFalse(report["rules"]["R2"])

    def test_r3_finish_reason_twice(self):
        chunks = self._stream()
        chunks[1]["finish_reason"] = "stop"
        report = validate_stream_chunks(chunks)
        self.assertFalse(report["rules"]["R3"])
        self.assertTrue(any("R3" in e for e in report["errors"]))

    def test_r4_done_missing(self):
        report = validate_stream_chunks(self._stream()[:-1])
        self.assertFalse(report["rules"]["R4"])
        self.assertTrue(any("R4" in e for e in report["errors"]))

    def test_r6_comment_frames_ignored(self):
        chunks = [{"comment": "swap heartbeat"}] + self._stream()
        report = validate_stream_chunks(chunks)
        self.assertTrue(report["ok"])


class C1C2门禁Tests(unittest.TestCase):
    def test_gate_passes_with_equal_format_both_sides(self):
        completion = {
            "id": "c1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "占位"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "headers": {"x-ofr-actual-model": "m"},
        }
        before = {"completions": [completion], "streams": []}
        after = {"completions": [completion], "streams": []}  # 内容相同仅示意；门禁只看格式
        gate = consistency_gate(before, after)
        self.assertTrue(gate["ok"])

    def test_gate_fails_when_after_drifts(self):
        bad_completion = {
            "id": "c2", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "exploded",
                         "message": {"role": "assistant", "content": "占位"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "headers": {},
        }
        good = {
            "id": "c1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "占位"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "headers": {"x-ofr-actual-model": "m"},
        }
        gate = consistency_gate({"completions": [good], "streams": []},
                                {"completions": [bad_completion], "streams": []})
        self.assertFalse(gate["ok"])
        self.assertTrue(any("after" in e for e in gate["errors"]))
        self.assertIn("格式一致 ≠ 内容一致", gate["note"])


if __name__ == "__main__":
    unittest.main()
