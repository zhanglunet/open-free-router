"""PRD-173/T4: BYOS 接入额度 / doctor / 账本既有通道（T1 §5，trend issue #868）。

三个对接点全部复用既有机制——账本不加列不改枚举、不新开表、额度不猜余额：

* 注册：``register_byos_providers`` 把每个已配置适配器经 env ``resolve``
  出的 ``ProviderConfig`` 注册进**内存中的** registry，使 BYOS 候选与免费
  候选走同一条 planner/executor 路径——从而天然共用既有额度归一化
  （``quota.py``）与成对换路审计（``failover_audit.py``）。
  ``registry.yaml`` 仍是唯一事实源：同名 provider 已存在时不覆盖，
  本模块也从不把解析结果写回磁盘（T1 §6 红线 1/2）。
* doctor：``doctor_checks`` 生成 ``byos`` 检查组（T1 §5 row 2）：每个启用
  适配器两条检查——env 在位（缺失 → warning ``byos_env_missing``）与探活
  可达（warning 级）；形状严格 ``{ok, severity, label, detail}``，
  ``detail`` 只含适配器名、状态枚举与 env 变量名，绝不含 env 值。
* refresh：``refresh.py`` 对 ``refresh_method="byos"`` 的 provider 一处
  判断即跳过（订阅额度不经目录轮询发现，T1 §5 sync 行）。

用量与换路记账无需 BYOS 特判：请求一旦走 executor，既有
``record_quota`` / ``_quota_context`` / ``AnalyticsAuditHook`` 路径自动
生效；无额度头的 BYOS 上游诚实落在 ``unknown`` / ``never_measured``
（宪法 §4.4 中性计分），本模块只为该口径补齐集成测试。
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Mapping

from open_free_router.byos import make_adapter
from open_free_router.byos.env import read_env

log = logging.getLogger(__name__)

# T1 §5: the env-presence status enum surfaced in doctor ``detail`` fields.
ENV_MISSING = "byos_env_missing"
ENV_PRESENT = "present"
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_SKIPPED = "skipped"


def register_byos_providers(
    registry,
    cfg,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve every configured BYOS adapter and register it in memory.

    Returns the names that were actually registered. Never raises into
    startup: a failing adapter is a warning plus a skip, and doctor will
    surface the reason. Existing ``registry.yaml`` entries win -- the
    registry file is the single source of truth for providers (T1 §5),
    so a same-name provider is never overwritten and nothing is saved back.
    """
    if not cfg.byos_enabled:
        return []
    source = os.environ if env is None else env
    registered: list[str] = []
    for name in cfg.byos_adapters:
        adapter = make_adapter(name)
        if adapter is None:
            # Config parsing already drops unknown names (T1 §3.4); this is
            # belt and braces, never a silent widening of the adapter set.
            continue
        if registry.get(name) is not None:
            log.info(
                "byos provider %s already defined in registry.yaml; left untouched",
                name,
            )
            continue
        try:
            provider = adapter.resolve(source)
        except Exception:
            log.warning("byos adapter %s resolve failed; skipped", name, exc_info=True)
            continue
        if provider is None:
            # Missing env etc. (T1 §3.5): not registered; doctor reports
            # ``byos_env_missing`` at warning level.
            continue
        registry.add_provider(provider)
        registered.append(name)
    return registered


def doctor_checks(
    cfg,
    env: Mapping[str, str] | None = None,
    probe: Callable[[object], dict] | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Build the ``byos`` doctor report group items (T1 §5 row 2).

    Two warning-level checks per enabled adapter: env presence and probe
    reachability. ``detail`` carries only the adapter name, a status enum
    and env variable *names* -- never a value (T1 §6 redline 2). ``probe``
    is injectable so tests stay offline; the default runs the adapter's
    real 1-token probe with ``timeout`` seconds. Probe is skipped when the
    env is incomplete or the adapter produced no registrable config.
    """
    if not cfg.byos_enabled:
        return []
    source = os.environ if env is None else env
    items: list[dict] = []
    for name in cfg.byos_adapters:
        adapter = make_adapter(name)
        if adapter is None:
            continue
        descriptor = adapter.descriptor
        missing = [env_name for env_name in descriptor.key_envs if not read_env(env_name, source)]
        if missing:
            items.append({
                "ok": False,
                "severity": "warning",
                "label": "byos env present",
                "detail": f"{name}: {ENV_MISSING} ({', '.join(missing)})",
            })
            items.append({
                "ok": False,
                "severity": "warning",
                "label": "byos probe reachable",
                "detail": f"{name}: {PROBE_SKIPPED}",
            })
            continue
        items.append({
            "ok": True,
            "severity": "ok",
            "label": "byos env present",
            "detail": f"{name}: {ENV_PRESENT}",
        })
        status = PROBE_FAILED
        try:
            provider = adapter.resolve(source)
            if provider is None:
                status = PROBE_SKIPPED
            else:
                run_probe = probe if probe is not None else (
                    lambda candidate: adapter.probe(candidate, timeout=timeout)
                )
                result = run_probe(provider)
                status = PROBE_OK if isinstance(result, dict) and result.get("ok") else PROBE_FAILED
        except Exception:
            log.warning("byos probe check failed for %s", name, exc_info=True)
            status = PROBE_FAILED
        items.append({
            "ok": status == PROBE_OK,
            "severity": "ok" if status == PROBE_OK else "warning",
            "label": "byos probe reachable",
            "detail": f"{name}: {status}",
        })
    return items
