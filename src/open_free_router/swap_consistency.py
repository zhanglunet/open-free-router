#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-230/T3（trend #1152）：热切换的上下文与输出格式一致性保障。

按 T1 设计（docs/plans/2026-10-26-local-model-hot-swap-design.md）§2/§3 实现：

  §2 上下文保持（判据 A~E）：
    - make_checkpoint()：切换前 checkpoint（swap_id/时间戳/活跃会话/last_request_seq/
      messages+system+params 快照哈希）——canonical JSON sha256，确定性；
    - context_replay_diff()：判据 A 全等回放——切换后 router 内会话记录与 checkpoint
      规范化 JSON 逐字段比对，§2.1 唯一允许变化的「模型标识」除外；不等即失败
      （调用方进 failed）；
    - probe_check()：判据 B 延续性探针——切换前写入的暗号 token 须出现在切换后回答中；
    - params_preserved()：判据 C 参数保持——未知键原样透传全等；
    - usage_continuous()：判据 D 统计连续——usage 累计跨切换连续累加，无清零无跳变
      （after ≥ before 且增量 ≤ 单请求上限不计跳变——本模块只断言不清零不减）；
    - abort_frame()：判据 E 终结合法——force 收尾补发合法终结帧（空 delta +
      finish_reason="aborted_by_swap" + data: [DONE]，帧含 x-ofr-swap-id）。
  §3 输出格式一致性：
    - validate_completion()：非流式响应 schema（§3.2-1 必填字段/finish_reason 词表/
      usage 三字段；model 值唯一豁免字段，headers x-ofr-actual-model 必附）；
    - validate_stream_chunks()：流式分片规则集 R1–R6 校验器（chunk 级报告）；
    - consistency_gate()：C1/C2 门禁——切换前后各一份样本跑同一校验，须同等 100%
      通过；任何一方失败 → 门禁不过（格式一致 ≠ 内容一致：不比对逐字文本）。

红线（§3.2）：禁止的切换特有畸形——[DONE] 缺失、role 帧重复、finish_reason 两次、
同一请求被两个模型各答半截；不承诺首 token 延迟与吞吐一致。
零网络；全部输入由调用方注入；canonical 校验确定性（T5 回放可复现）。
测试：tests/test_prd230_t3_consistency.py
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: §2.1 唯一允许变化的字段（切换后指向新模型）
MODEL_EXEMPT_FIELDS = ("model",)
#: §2.2-5 OFR 扩展 finish_reason 枚举（PRD-060 归一层透传）
ABORT_FINISH_REASON = "aborted_by_swap"
#: §3.2-1 非流式 finish_reason 词表
FINISH_REASONS = ("stop", "length", "tool_calls", "content_filter", ABORT_FINISH_REASON)
#: §3.2-1 usage 必填三字段
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class ConsistencyError(ValueError):
    """一致性输入/输出违规。"""


def canonical(obj: Any) -> str:
    """规范化 JSON（键排序、紧凑分隔符）——哈希与比对统一用这一份。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# §2 checkpoint 与判据 A~E
# ---------------------------------------------------------------------------

def make_checkpoint(swap_id: str, timestamp: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """切换前 checkpoint（§2.2-2）：每会话 messages+system+params 快照哈希。

    session 形态：{session_id, model, system, messages, params, last_request_seq,
    usage}；缺 session_id/messages 抛错（checkpoint 写盘失败语义由 T2 precheck
    负责，本模块拒绝构造残缺 checkpoint）。
    """
    entries = []
    for session in sessions:
        for field in ("session_id", "messages"):
            if field not in session:
                raise ConsistencyError(f"checkpoint 会话缺必填字段 {field}")
        payload = {
            "messages": session["messages"],
            "system": session.get("system"),
            "params": session.get("params", {}),
        }
        entries.append({
            "session_id": session["session_id"],
            "last_request_seq": session.get("last_request_seq", 0),
            "snapshot_hash": _sha16(canonical(payload)),
            "usage": dict(session.get("usage", {})),
        })
    return {
        "schema": "swap-checkpoint/v1",
        "swap_id": swap_id,
        "timestamp": timestamp,
        "sessions": entries,
    }


def context_replay_diff(checkpoint: dict[str, Any],
                        sessions_after: list[dict[str, Any]]) -> list[str]:
    """判据 A 全等回放：checkpoint vs 切换后会话记录，规范化逐字段全等。

    唯一豁免：model 标识如实变更（§2.1 #5）。返回错误清单（空 = 上下文没丢）。
    """
    errors: list[str] = []
    before = {e["session_id"]: e for e in checkpoint.get("sessions", [])}
    after = {s["session_id"]: s for s in sessions_after}
    for session_id, entry in before.items():
        if session_id not in after:
            errors.append(f"{session_id}: 切换后会话记录缺失（上下文丢失）")
            continue
        s = after[session_id]
        payload_after = {
            "messages": s.get("messages"),
            "system": s.get("system"),
            "params": s.get("params", {}),
        }
        if _sha16(canonical(payload_after)) != entry["snapshot_hash"]:
            errors.append(f"{session_id}: 快照哈希不一致（messages/system/params 被改动）")
        if s.get("last_request_seq", 0) < entry["last_request_seq"]:
            errors.append(f"{session_id}: last_request_seq 回退"
                          f"（{s.get('last_request_seq')} < {entry['last_request_seq']}）")
    for session_id in set(after) - set(before):
        errors.append(f"{session_id}: 切换后多出 checkpoint 之外的会话（来源不明）")
    return errors


def probe_check(expected_token: str, reply: str) -> bool:
    """判据 B 延续性探针：切换前写入的暗号须出现在切换后回答中。"""
    if not expected_token or not isinstance(reply, str):
        return False
    return expected_token in reply


def params_preserved(params_before: dict[str, Any], params_after: dict[str, Any]) -> bool:
    """判据 C 参数保持：未知键原样透传，全等比较（model 字段不在 params 内）。"""
    return canonical(params_before) == canonical(params_after)


def usage_continuous(usage_before: dict[str, int], usage_after: dict[str, int]) -> bool:
    """判据 D 统计连续：累计值不清零、不减少（跨切换连续累加）。"""
    for field in USAGE_FIELDS:
        after = usage_after.get(field, 0)
        before = usage_before.get(field, 0)
        if after < before:
            return False  # 清零或跳变（回退）
    return True


def abort_frame(swap_id: str) -> str:
    """判据 E：force 收尾的合法 SSE 终结帧（空 delta + 扩展 finish_reason + [DONE]）。

    客户端按未知 finish_reason 处理为终止即可；帧携带 x-ofr-swap-id（§2.2-5）。
    """
    frame = {
        "id": f"chatcmpl-swap-{swap_id}",
        "object": "chat.completion.chunk",
        "delta": {},
        "finish_reason": ABORT_FINISH_REASON,
    }
    return (
        f": swap {swap_id} force-drain\n"
        f"x-ofr-swap-id: {swap_id}\n"
        f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    )


# ---------------------------------------------------------------------------
# §3 输出格式一致性
# ---------------------------------------------------------------------------

def validate_completion(resp: dict[str, Any], *, swap_id: str | None = None) -> list[str]:
    """非流式响应 schema 校验（§3.2-1）：返回错误清单（空 = 合格）。

    model 值为唯一豁免字段（如实返回当前模型名）；headers 侧要求
    x-ofr-actual-model 存在（resp["headers"] 注入形态）。
    """
    errors: list[str] = []
    for field in ("id", "object", "created", "model", "choices", "usage"):
        if field not in resp:
            errors.append(f"缺必填字段 {field}")
    if resp.get("object") != "chat.completion":
        errors.append(f"object 必须为 chat.completion，收到 {resp.get('object')!r}")
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        errors.append("choices 必须为非空数组")
    else:
        for index, choice in enumerate(choices):
            if not isinstance(choice.get("index"), int):
                errors.append(f"choices[{index}].index 必须为 int")
            message = choice.get("message") or {}
            if message.get("role") != "assistant":
                errors.append(f"choices[{index}].message.role 必须为 assistant")
            if "content" not in message and "tool_calls" not in message:
                errors.append(f"choices[{index}].message 缺 content/tool_calls")
            if choice.get("finish_reason") not in FINISH_REASONS:
                errors.append(f"choices[{index}].finish_reason 非法："
                              f"{choice.get('finish_reason')!r}（词表 {FINISH_REASONS}）")
    usage = resp.get("usage")
    if not isinstance(usage, dict):
        errors.append("usage 必须为对象")
    else:
        for field in USAGE_FIELDS:
            if not isinstance(usage.get(field), int):
                errors.append(f"usage.{field} 必填且为 int")
    headers = resp.get("headers") or {}
    if "x-ofr-actual-model" not in headers:
        errors.append("headers 缺 x-ofr-actual-model（§3.2-1 必附）")
    if swap_id is not None and headers.get("x-ofr-swap-id") != swap_id:
        errors.append("切换后的响应须附 x-ofr-swap-id")
    return errors


def validate_stream_chunks(chunks: list[Any]) -> dict[str, Any]:
    """流式分片 R1–R6 校验器（§3.2-2）：返回 {ok, rules, errors}（chunk 级）。

    chunks 为已解析的 data 帧序列（json 帧或字符串 "[DONE]"）；注释行/心跳帧
    由调用方剥除或以 {"comment": ...} 形态传入（R6 允许存在不影响解析）。
    """
    errors: list[str] = []
    rules = {"R1": True, "R2": True, "R3": True, "R4": True, "R5": True, "R6": True}
    data_frames = [c for c in chunks if not (isinstance(c, dict) and "comment" in c)]
    parsed = []
    done_count = 0
    for chunk in data_frames:
        if chunk == "[DONE]" or (isinstance(chunk, dict) and chunk.get("raw") == "[DONE]"):
            done_count += 1
            parsed.append(None)
            continue
        if isinstance(chunk, str):
            try:
                parsed.append(json.loads(chunk))
            except json.JSONDecodeError:
                errors.append(f"data 帧不可解析：{chunk[:60]!r}")
                parsed.append(None)
            continue
        parsed.append(chunk)

    role_seen = 0
    finish_seen = 0
    last_finish_index = -1
    for index, frame in enumerate(parsed):
        if frame is None:
            continue
        if frame.get("object") not in (None, "chat.completion.chunk"):
            errors.append(f"chunk[{index}] object 非法：{frame.get('object')!r}")
            rules["R2"] = False
        delta = frame.get("delta")
        if not isinstance(delta, dict):
            errors.append(f"chunk[{index}] 缺 delta 对象")
            rules["R2"] = False
            continue
        if "role" in delta:
            role_seen += 1
            if index != 0:
                errors.append(f"chunk[{index}] role 帧重复（R1：仅首个 data chunk 可含 role）")
                rules["R1"] = False
        if index > 0:
            allowed = set(delta) <= {"content", "tool_calls"}
            if not allowed:
                errors.append(f"chunk[{index}] delta 含越界字段：{sorted(delta)}（R2）")
                rules["R2"] = False
        if frame.get("finish_reason") is not None:
            finish_seen += 1
            last_finish_index = index
            if frame["finish_reason"] not in FINISH_REASONS:
                errors.append(f"chunk[{index}] finish_reason 非法：{frame['finish_reason']!r}")
                rules["R3"] = False
    if finish_seen > 1:
        errors.append(f"finish_reason 出现 {finish_seen} 次（R3：至多最后一个 data chunk）")
        rules["R3"] = False
    if done_count != 1:
        errors.append(f"[DONE] 出现 {done_count} 次（R4：必须存在且唯一）")
        rules["R4"] = False
    if last_finish_index != -1 and last_finish_index != len(parsed) - 2:
        errors.append("finish_reason 不在最后一个 data chunk（R3）")
        rules["R3"] = False
    return {"ok": not errors, "rules": rules, "errors": errors}


def consistency_gate(before: dict[str, list], after: dict[str, list]) -> dict[str, Any]:
    """C1/C2 门禁：切换前后样本各跑同一校验，同等 100% 通过（§3.3）。

    before/after 形态：{"completions": [resp...], "streams": [chunks...]}；
    格式一致 ≠ 内容一致：不比对逐字文本（§3.2 红线）。
    """
    errors: list[str] = []
    for side, sample in (("before", before), ("after", after)):
        for index, resp in enumerate(sample.get("completions") or []):
            for error in validate_completion(resp):
                errors.append(f"{side}.completions[{index}]: {error}")
        for index, chunks in enumerate(sample.get("streams") or []):
            report = validate_stream_chunks(chunks)
            for error in report["errors"]:
                errors.append(f"{side}.streams[{index}]: {error}")
    return {
        "schema": "swap-consistency-gate/v1",
        "ok": not errors,
        "errors": errors,
        "note": "C1 schema 抽检 + C2 流序列校验，切换前后同等 100% 通过；"
                "格式一致 ≠ 内容一致（不比对逐字文本，性能差异不计入）",
    }
