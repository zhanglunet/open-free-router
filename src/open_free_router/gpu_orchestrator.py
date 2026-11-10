# -*- coding: utf-8 -*-
"""PRD-336/T2（trend issue #1615 的 T2 环节）：GPU 资源编排最小实现。

按 T1 设计（docs/plans/2026-11-09-prd-336-gpu-orchestra-design.md）§3/§4/§4.3
实现：

  - GpuLedger：gpu-ledger/v1 声明式账本（§3.1）——每模型 vram_mb 由调用方
    注入零探测；free_est = total − Σ reserved 纯算术；
  - plan(target_model, ledger)：预留判定三路（§3.2）——直接换入 hot_swap /
    先卸后换（victim=非在服且 LRU 最旧，确定性）→ unload_then_swap / 
    GPU_INFEASIBLE 显形不硬换。**编排器只出建议**——执行归既有 hot_swap
    状态机（调用方按建议执行，零设备调用零自动换路，R1/T1 §4.3）；
  - 卸载顺序确定性（§4.1）：非在服优先 → LRU → model_id 字典序；
  - 回退判据表（§4.2）随建议输出（判据逐条定死，触发即回退——执行归既有
    hot_swap 回滚路径）；
  - apply_result：执行结果回填账本（healthy/rolled_back），append-only。

零设备调用零网络（R1/T1）；示例全虚构占位。
测试：tests/test_prd336_t2_gpu_orchestrator.py
"""
from __future__ import annotations

from typing import Any

LEDGER_SCHEMA = "gpu-ledger/v1"


class OrchestratorError(ValueError):
    """编排输入非法（账本/参数），显形拒绝不猜测。"""


def new_ledger(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    """构建账本：gpus=[{gpu_id, total_vram_mb}]（容量由调用方声明零探测）。"""
    return {
        "schema": LEDGER_SCHEMA,
        "gpus": [{"gpu_id": g["gpu_id"], "total_vram_mb": g["total_vram_mb"],
                  "reserved": {}} for g in gpus],
        "models": [],
    }


def _gpu_of(ledger: dict[str, Any], gpu_id: str) -> dict[str, Any]:
    for g in ledger["gpus"]:
        if g["gpu_id"] == gpu_id:
            return g
    raise OrchestratorError(f"账本无此 GPU：{gpu_id!r}")


def reserve(ledger: dict[str, Any], model: str, gpu_id: str, vram_mb: int) -> None:
    """载入成功后回填账本：model 占 gpu_id 的 vram_mb。"""
    gpu = _gpu_of(ledger, gpu_id)
    gpu["reserved"][model] = vram_mb
    ledger["models"].append({"model": model, "vram_mb": vram_mb, "gpu_id": gpu_id,
                             "state": "resident"})


def release(ledger: dict[str, Any], model: str, gpu_id: str) -> None:
    """卸载后回填账本：model 从 gpu_id 释放。"""
    gpu = _gpu_of(ledger, gpu_id)
    gpu["reserved"].pop(model, None)
    ledger["models"] = [m for m in ledger["models"]
                        if not (m["model"] == model and m["gpu_id"] == gpu_id)]


def free_est(ledger: dict[str, Any], gpu_id: str) -> int:
    gpu = _gpu_of(ledger, gpu_id)
    used = sum(gpu["reserved"].values())
    return gpu["total_vram_mb"] - used


def plan(target_model: str, target_vram_mb: int, ledger: dict[str, Any], *,
         current_model: str | None = None,
         last_used: dict[str, str] | None = None) -> dict[str, Any]:
    """预留判定三路（§3.2，确定性）：

    1. free 足够 → {action: hot_swap}；
    2. free 不足但卸掉某非在服 LRU victim 后满足 → {action: unload_then_swap,
       victims=[victim]}（victim 选择：非当前在服 → LRU 最旧 → 字典序）；
    3. 无论如何不满足 → {action: infeasible}（GPU_INFEASIBLE 显形不硬换）。

    回退判据表随建议输出（§4.2，触发即回退——执行归既有 hot_swap 回滚）。
    """
    # 1) 任一卡 free 直接足够 → hot_swap（多卡时按 gpu_id 字典序找第一张）
    for gpu in sorted(ledger["gpus"], key=lambda g: g["gpu_id"]):
        if free_est(ledger, gpu["gpu_id"]) >= target_vram_mb:
            return _plan_hot_swap(gpu["gpu_id"], target_model, target_vram_mb)
    # 2) 先卸后换：单卡内按 LRU 卸非在服 victim 直到足够（确定性；当前在服
    #    模型永不卸——回退底座 R2）；跨卡切分不支持（T1 明确不做）
    for gpu in sorted(ledger["gpus"], key=lambda g: g["gpu_id"]):
        victims = _lru_victims(ledger, gpu["gpu_id"],
                               exclude={current_model} if current_model else set(),
                               last_used=last_used or {})
        freed = 0
        chosen: list[dict[str, Any]] = []
        for v in victims:
            freed += v["vram_mb"]
            chosen.append(v)
            if freed >= target_vram_mb:
                break
        if freed >= target_vram_mb:
            return {
                "action": "unload_then_swap",
                "gpu_id": gpu["gpu_id"],
                "victims": chosen,
                "target_model": target_model, "target_vram_mb": target_vram_mb,
                "fallback_criteria": _fallback_criteria(),
                "note": "先卸后换——victim 为非在服 LRU 最旧（确定性）；"
                        "执行归既有 hot_swap 状态机（编排器只出建议）",
            }
    return {
        "action": "infeasible",
        "target_model": target_model, "target_vram_mb": target_vram_mb,
        "fallback_criteria": _fallback_criteria(),
        "note": "GPU_INFEASIBLE：目标显存需求超单卡容量——显形不硬换（§3.2）",
    }


def _lru_victims(ledger: dict[str, Any], gpu_id: str,
                 exclude: set[str], last_used: dict[str, str]) -> list[dict[str, Any]]:
    """§4.1 卸载顺序：非当前在服优先 → LRU 最旧 → model_id 字典序。"""
    gpu = next(g for g in ledger["gpus"] if g["gpu_id"] == gpu_id)
    rows = []
    for model, vram in gpu["reserved"].items():
        if model in exclude:
            continue
        rows.append({"model": model, "vram_mb": vram,
                     "last_used_at": (last_used or {}).get(model, "")})
    rows.sort(key=lambda r: (r["last_used_at"], r["model"]))
    return rows


def _fallback_criteria() -> list[dict[str, str]]:
    return [
        {"criterion": "换入健康探针失败", "threshold": "retries 全败",
         "action": "自动回滚（hot_swap 既有顺序）"},
        {"criterion": "换入超时", "threshold": "LOAD_TIMEOUT_MS（默认 120s）",
         "action": "回退建议 + 显形"},
        {"criterion": "回退自身失败", "threshold": "原模型 load 失败",
         "action": "failed 显形（零伪装），账本记 fallback_broken 供人工介入"},
    ]


def _plan_hot_swap(gpu_id: str, target_model: str, target_vram_mb: int) -> dict[str, Any]:
    return {
        "action": "hot_swap", "gpu_id": gpu_id,
        "target_model": target_model, "target_vram_mb": target_vram_mb,
        "fallback_criteria": _fallback_criteria(),
        "note": "free 足够——直接换入；执行归既有 hot_swap 状态机（编排器只出建议）",
    }
