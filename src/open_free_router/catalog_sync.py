# -*- coding: utf-8 -*-
"""PRD-466/T2（trend issue #2251 的 T2 环节）：签名目录拉取与验证。

按 T1 设计（docs/plans/2026-11-17-signed-catalog-design.md）§2.2/§3/§6 落地：
  - 信任模型（§3）：Ed25519 公钥钉死在本模块常量（私钥从不离源站）；
    OFR_CATALOG_PUBKEY 可覆盖（自托管场景）；OFR_CATALOG_SYNC_DISABLED=1
    关总开关（本地缓存继续服务）；
  - 验证顺序（§2.2 写死）：先验签、验签通过才解析 JSON——验签失败连解析
    都不做，整份丢弃 + 大声报错；缺签名头同样丢弃（没有无签名的降级）；
  - 结构校验 isCatalog（§2.1）：顶层形状不对整份丢弃，不修补；
  - 版本底线（§4）：version < MIN_CATALOG_VERSION → skipped_older；
    == 已应用版本 → up_to_date；> 才走应用（T3）；
  - 失败降级（§6）：超时/非 2xx/形状非法/缺签名/验签失败 → 整份丢弃，
    本地目录继续服务；重试按退避（默认 ≥5 分钟，可注入），同步不阻塞
    请求路径——同步挂掉时网关表现与「没有同步功能」完全一致；
  - fetch_fn 注入（默认 requests.get，20 秒超时）；测试注入 fake；
  - 状态文件 data/catalog-state.json：applied_version/applied_tier/
    applied_json（已验证整份文档）/last_sync_ms/last_error（启动重放用）。
零 LLM 零子进程；网络仅限目录源拉取（注入点可替换）；示例全虚构占位。
测试：tests/test_prd466_t2_catalog_sync.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from open_free_router.ed25519_verify import verify as ed25519_verify

SYNC_SCHEMA = "catalog-sync/v1"
CATALOG_PUBKEY_PINNED = (
    "3d4017c3e8433952dadca9ef42e1c72e"
    "6c8ff2d4a4b7de5a9b9d2f9d7c4e8b1a"      # 占位公钥（钉死值，部署时替换为真实钥）
)
DEFAULT_BASE_URL = "https://example.invalid/open-free-router/catalog（占位）"
MIN_CATALOG_VERSION = "2026.11.17"          # 版本底线：随基线发布递增
STATE_VERSION = "catalog-state/v1"
_VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(-\d+)?$")
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RETRY_BACKOFF_SECONDS = 300          # 失败重试退避，不低于 5 分钟

SIGNATURE_HEADER = "x-catalog-signature"


class CatalogSyncError(ValueError):
    """目录同步违规——显形拒绝（大声报错，不静默吞）。"""


def _pubkey() -> str:
    return os.environ.get("OFR_CATALOG_PUBKEY", CATALOG_PUBKEY_PINNED).strip()


def _sync_disabled() -> bool:
    return os.environ.get("OFR_CATALOG_SYNC_DISABLED", "") == "1"


def is_catalog(doc: Any) -> bool:
    """§2.1 结构校验：顶层形状不对 → False（整份丢弃，不修补）。"""
    if not isinstance(doc, dict):
        return False
    version = doc.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        return False
    providers = doc.get("providers")
    if not isinstance(providers, list):
        return False
    for p in providers:
        if not isinstance(p, dict) or not isinstance(p.get("platform"), str) \
                or not isinstance(p.get("models"), list):
            return False
        for m in p["models"]:
            if not isinstance(m, dict) or not isinstance(m.get("model_id"), str):
                return False
    quirks = doc.get("quirks")
    if not isinstance(quirks, list):
        return False
    return isinstance(doc.get("generated_at"), str)


def verify_catalog_bytes(body: bytes, signature_b64: str | None,
                         pubkey_hex: str) -> dict[str, Any]:
    """§2.2 验证顺序写死：先验签、通过才解析 JSON。任一失败整份丢弃。"""
    if not signature_b64:
        raise CatalogSyncError(
            "catalog signature verification FAILED — discarding response "
            "(missing signature header)")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise CatalogSyncError(
            "catalog signature verification FAILED — discarding response "
            f"(signature not base64: {exc})") from exc
    if not ed25519_verify(bytes.fromhex(pubkey_hex), body, signature):
        raise CatalogSyncError(
            "catalog signature verification FAILED — discarding response")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogSyncError(
            f"catalog body failed to parse after signature check: {exc}") from exc
    if not is_catalog(doc):
        raise CatalogSyncError(
            "catalog body failed structural validation — discarding response")
    return doc


def compare_versions(a: str, b: str) -> int:
    """版本比较（§4）：按 YYYY.MM.DD[-n] 字典序；返回 1/0/-1；非法 → ValueError。"""
    for v in (a, b):
        if not isinstance(v, str) or not _VERSION_RE.match(v):
            raise CatalogSyncError(f"非法目录版本号 {v!r}")
    return (a > b) - (a < b)


def version_decision(version: str, min_version: str = MIN_CATALOG_VERSION,
                     applied_version: str | None = None) -> str:
    """§4 版本底线三态：skipped_older / up_to_date / apply。

    回滚守卫（PRD 验收点②「旧版本不回滚」）：version < applied_version
    （即使 ≥ MIN_CATALOG_VERSION）也是 skipped_older——旧快照永远不能
    把已上线的新版本目录滚回去。
    """
    if applied_version is not None and compare_versions(version,
                                                        applied_version) < 0:
        return "skipped_older"
    if compare_versions(version, min_version) < 0:
        return "skipped_older"
    if applied_version is not None and version == applied_version:
        return "up_to_date"
    return "apply"


def load_state(state_path: str | Path | None) -> dict[str, Any]:
    path = Path(state_path) if state_path else None
    if path is None or not path.exists():
        return {"schema": STATE_VERSION, "applied_version": None,
                "applied_tier": None, "applied_json": None,
                "last_sync_ms": None, "last_error": None}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("schema", STATE_VERSION)
    for k in ("applied_version", "applied_tier", "applied_json",
              "last_sync_ms", "last_error"):
        data.setdefault(k, None)
    return data


def save_state(state_path: str | Path | None, state: dict[str, Any]) -> None:
    if state_path is None:
        return
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True,
                              indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)          # 原子替换（T3 事务语义的状态文件部分）


def fetch_and_verify(*, base_url: str | None = None,
                     applied_version: str | None = None,
                     fetch_fn: Callable[[str], dict[str, Any]] | None = None,
                     timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """拉取 → 验签 → 结构校验 → 版本判定。任何失败结构化返回（不抛、
    不断线——本地目录继续服务），last_error 写明原因（大声报错语义）。"""
    if _sync_disabled():
        return {"schema": SYNC_SCHEMA, "outcome": "disabled",
                "detail": "OFR_CATALOG_SYNC_DISABLED=1——本地缓存继续服务",
                "doc": None}
    url = base_url or DEFAULT_BASE_URL
    if not url.lower().startswith("https://"):
        return {"schema": SYNC_SCHEMA, "outcome": "failed",
                "detail": f"目录源必须 https：{url!r}", "doc": None}
    fetch = fetch_fn or _default_fetch
    try:
        response = fetch(url)
    except Exception as exc:    # 网络/超时/注入 fake 的任何异常 → 降级
        return {"schema": SYNC_SCHEMA, "outcome": "degraded",
                "detail": f"fetch failed: {exc.__class__.__name__}",
                "doc": None}
    body = response.get("body")
    if response.get("status") != 200 or not isinstance(body, (bytes, str)):
        return {"schema": SYNC_SCHEMA, "outcome": "degraded",
                "detail": f"fetch non-2xx: status={response.get('status')}",
                "doc": None}
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        doc = verify_catalog_bytes(body_bytes,
                                   response.get("headers", {}).get(
                                       SIGNATURE_HEADER), _pubkey())
    except CatalogSyncError as exc:
        return {"schema": SYNC_SCHEMA, "outcome": "failed",
                "detail": str(exc), "doc": None}
    version = doc["version"]
    decision = version_decision(version, applied_version=applied_version)
    return {"schema": SYNC_SCHEMA, "outcome": decision,
            "detail": f"version={version}", "doc": doc}


def _default_fetch(url: str) -> dict[str, Any]:
    """默认拉取：requests，20 秒超时（§6）。测试注入 fetch_fn 替换。"""
    import requests   # 既有依赖（pyproject 已声明），延迟导入避免硬依赖
    resp = requests.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
    return {"status": resp.status_code, "body": resp.content,
            "headers": dict(resp.headers)}


def run_sync(*, state_path: str | Path | None = None,
             base_url: str | None = None, now_ms: int | None = None,
             fetch_fn: Callable[[str], dict[str, Any]] | None = None,
             apply_fn: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """一次同步：读状态 → 拉取验证 → 版本判定 → apply_fn 应用（T3 注入）
    → 状态文件更新（原子）。失败写 last_error，本地继续服务。"""
    state = load_state(state_path)
    result = fetch_and_verify(base_url=base_url,
                              applied_version=state.get("applied_version"),
                              fetch_fn=fetch_fn)
    outcome = result["outcome"]
    if outcome == "apply" and result.get("doc"):
        doc = result["doc"]
        applied = True
        if apply_fn is not None:
            applied = bool(apply_fn(doc))
        if applied:
            state["applied_version"] = doc["version"]
            state["applied_tier"] = doc.get("tier")
            state["applied_json"] = doc
            state["last_error"] = None
        else:
            state["last_error"] = "apply_fn rejected the document"
            state["last_sync_ms"] = now_ms
            save_state(state_path, state)
            return {"schema": SYNC_SCHEMA, "outcome": "failed",
                    "detail": state["last_error"], "doc": None}
    elif outcome in ("failed", "degraded"):
        state["last_error"] = result["detail"]   # 大声记录，不静默
    elif outcome in ("up_to_date", "skipped_older", "disabled"):
        state["last_error"] = None
    state["last_sync_ms"] = now_ms
    save_state(state_path, state)
    return result


def retry_backoff_seconds(attempt: int = 1) -> int:
    """失败重试退避（§6）：不低于 5 分钟，按次线性翻倍、上限 30 分钟。"""
    return max(DEFAULT_RETRY_BACKOFF_SECONDS,
               min(DEFAULT_RETRY_BACKOFF_SECONDS * 2 ** max(0, attempt - 1),
                   1800))


def build_catalog_source_url(since_version: str | None = None) -> str:
    """条件请求（§6）：带 ?since=<已应用版本>，源站可返回 304。"""
    url = os.environ.get("OFR_CATALOG_BASE_URL", DEFAULT_BASE_URL)
    if since_version:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}since={since_version}"
    return url
