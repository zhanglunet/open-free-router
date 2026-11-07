# -*- coding: utf-8 -*-
"""PRD-294/T2（trend issue #1472）：路由指令 @后端引用解析最小实现。

按 T1 设计（docs/plans/2026-11-04-prd-294-at-backend-ref-design.md）§3/§4/§6
实现：

  - parse(raw)：@后端名 单形态——禁止形态（策略后缀 #failover/权重修饰 +N/
    组合引用 @a,@b/双写）抛 RefSyntaxError；
  - resolve(raw, backends, health_lookup)：候选集注入纯函数（零网络零探活——
    health_lookup 为调用方注入的只读查询，返回 healthy|degraded|down|unknown，
    本模块不主动探活不等待不重试，R1）；精确匹配，候选数裁决：1 →
    Resolution（**health 为报告字段**，unknown 合法显形）；0 → REF_NOT_FOUND；
    >1 → REF_AMBIGUOUS。**二值返回** Resolution | Refusal；
  - **不自动换路的解析面保证**：本模块零 failover/executor/hot_swap 导入
    （T3 AST 断言目标，T1 §5）——health 只报告不动作；
  - RefAudit：解析审计（§6）——append-only、health_status 带时点快照
    （checked_at）、成败都记。

零网络；示例全虚构占位。
测试：tests/test_prd294_t2_backend_ref.py
"""
from __future__ import annotations

import re
from typing import Any, Callable

REF_SCHEMA = "ref-resolution/v1"
HEALTH_STATES = ("healthy", "degraded", "down", "unknown")

#: 错误码封闭词表（PRD-294/T3，trend #1473）：refused 的 error_code 只能取自本表，
#: 新增错误必须先改词表（穷举防漂移；测试做封闭性机判）。
ERROR_CODES = ("REF_NOT_FOUND", "REF_AMBIGUOUS", "REF_SYNTAX_ERROR")

NAME_RE = re.compile(r"^@(?P<name>[^@\s+#%=:;,，]+)$")


class RefSyntaxError(ValueError):
    """§3.2 禁止形态——出现即拒绝，不尝试理解。"""


def parse(raw: str) -> str:
    """@语法解析：返回后端名；禁止形态抛 RefSyntaxError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise RefSyntaxError(f"引用为空或非字符串：{raw!r}")
    token = raw.strip()
    if token.startswith("@@") or "@" in token[1:] or '"' in token:
        raise RefSyntaxError(f"禁止形态（双写/引号/内嵌@）：{raw!r}（§3.2）")
    m = NAME_RE.match(token)
    if not m:
        if re.search(r"#\w+", token):
            raise RefSyntaxError(f"禁止形态（策略后缀——路由语义归 PRD-257）：{raw!r}（§3.2）")
        if re.search(r"[+#%=:]", token):
            raise RefSyntaxError(f"禁止形态（权重/倍率修饰）：{raw!r}（§3.2）")
        raise RefSyntaxError(f"禁止形态（缺 @/含空白/组合引用）：{raw!r}（§3.2）")
    return m.group("name")


def resolve(raw: str, backends: list[dict[str, Any]], *,
            health_lookup: Callable[[str], str] | None = None) -> dict[str, Any]:
    """@名→id+健康 解析（§4.2）：精确匹配 + 候选数裁决 + 健康只读报告。

    backends：[{"name","backend_id","models"?…}]（注入）；
    health_lookup(backend_id) → healthy|degraded|down|unknown（注入只读）。
    返回 Resolution | Refusal（二值，T1 §5）。
    """
    name = parse(raw)
    hits = [b for b in (backends or []) if b.get("name") == name]
    if len(hits) > 1:
        return _refusal(raw, "REF_AMBIGUOUS",
                        [b.get("backend_id") or b.get("name") for b in hits],
                        "同名后端跨命名空间——停下问（§4.2）")
    if not hits:
        return _refusal(raw, "REF_NOT_FOUND", [],
                        "无此后端——可用清单摘要见候选集（§5）")
    entry = hits[0]
    backend_id = entry.get("backend_id") or entry["name"]
    status = "unknown"
    if health_lookup is not None:
        status = health_lookup(backend_id) or "unknown"
        if status not in HEALTH_STATES:
            status = "unknown"  # 非词表值不透传（显形为 unknown，不猜测）
    return {
        "schema": REF_SCHEMA,
        "raw": raw,
        "kind": "backend",
        "backend_id": backend_id,
        "health": {"status": status, "checked_at": None,
                   "source": "availability（注入只读）"},
        "meta_summary": {"models": list(entry.get("models") or []),
                         "byok": bool(entry.get("byok"))},
        "resolution": {"status": "resolved", "candidates": 1, "strategy": "exact"},
    }


def _refusal(raw: str, code: str, candidates: list, detail: str) -> dict[str, Any]:
    if code not in ERROR_CODES:  # 词表外错误码不许溜进产物（封闭性）
        raise ValueError(f"未登记错误码：{code}（词表={ERROR_CODES}）")
    return {
        "schema": REF_SCHEMA, "raw": raw, "kind": "backend",
        "backend_id": None,
        "health": None, "meta_summary": None,
        "resolution": {"status": "refused", "error_code": code,
                       "candidates": candidates, "detail": detail},
    }


class RefAudit:
    """解析审计（§6）：append-only、health 带时点快照、成败都记。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def log(self, raw: str, outcome: dict[str, Any] | None, *, at: str) -> dict[str, Any]:
        res = (outcome or {}).get("resolution", {})
        entry: dict[str, Any] = {"ts": at, "raw": raw,
                                 "kind": (outcome or {}).get("kind")}
        if res.get("status") == "resolved":
            entry.update({
                "status": "resolved", "backend_id": outcome.get("backend_id"),
                "health_status": (outcome.get("health") or {}).get("status"),
                "checked_at": (outcome.get("health") or {}).get("checked_at") or at,
            })
        else:
            entry.update({"status": "refused", "candidates": res.get("candidates", []),
                          "error_code": res.get("error_code"),
                          "detail": res.get("detail")})
        self.records.append(entry)  # 只增不删
        return dict(entry)

    def resolve_logged(self, raw: str, backends: list[dict[str, Any]], *, at: str,
                       health_lookup: Callable[[str], str] | None = None) -> dict[str, Any]:
        """一步解析+留痕（语法错也记审计）。"""
        try:
            outcome = resolve(raw, backends, health_lookup=health_lookup)
        except RefSyntaxError as exc:
            entry = {"ts": at, "raw": raw, "kind": None, "status": "refused",
                     "candidates": [], "error_code": "REF_SYNTAX_ERROR",
                     "detail": str(exc)}
            self.records.append(entry)
            return {"refused": True, "error_code": "REF_SYNTAX_ERROR",
                    "detail": str(exc), "audit": entry}
        entry = self.log(raw, outcome, at=at)
        if outcome["resolution"]["status"] == "refused":
            return {"refused": True,
                    "error_code": outcome["resolution"]["error_code"],
                    "detail": outcome["resolution"].get("detail"), "audit": entry}
        return {"refused": False, "resolution": outcome, "audit": entry}
