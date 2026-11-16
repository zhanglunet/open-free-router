# -*- coding: utf-8 -*-
"""PRD-473/T2~T4（trend issue #2286/#2287/#2288 的 T2+T3+T4 环节）：
doctor 诊断 + setup 生成器 + 零留存启动器。

按 T1 设计（docs/plans/2026-11-17-cli-doctor-design.md）§1~§3 落地：
  - doctor（T2）：四项一键自检（配置/密钥/连通性/目录新鲜度），结论可操作；
  - setup 生成器（T3）：备份已有配置（*.bak-时间戳），只填空缺键不覆盖，
    生成 registry.yaml 草稿（从 catalog 注入）；
  - 零留存启动器（T4）：凭据只注入子进程 env（subprocess.Popen），不落盘，
    不进日志，进程结束即失效——零留存启动器不替用户保管密钥。
零 LLM 零网络（probe_fn/connectivity_fn 注入）；示例全虚构占位。
测试：tests/test_prd473_t2_cli_doctor.py
"""
from __future__ import annotations

import copy
import json
import os
import subprocess  # 仅零留存启动器用（T4 设计写死）；产品代码零子进程
from pathlib import Path
from typing import Any, Callable

DOCTOR_SCHEMA = "cli-doctor/v1"
SETUP_SCHEMA = "cli-setup/v1"
LAUNCHER_SCHEMA = "cli-launcher/v1"
ERROR_CODES = ("DOCTOR_INCOMPLETE", "SETUP_BACKUP_EXISTS",
               "LAUNCHER_KEY_NOT_FOUND")


class DoctorError(ValueError):
    """doctor/setup/launcher 违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise DoctorError(f"{code}：{detail}")


def run_doctor(registry: dict[str, Any] | None,
               env: dict[str, str] | None = None,
               connectivity_fn: Callable[[str], bool] | None = None,
               catalog_version: str | None = None,
               min_catalog_version: str = "2026.11.17") -> dict[str, Any]:
    """T2 doctor 四项一键自检（结论可操作）。"""
    env = env or os.environ
    checks = []
    # 1) config
    if isinstance(registry, dict) and registry.get("providers"):
        checks.append({"check": "config", "status": "ok",
                       "advice": ""})
    elif isinstance(registry, dict):
        checks.append({"check": "config", "status": "warning",
                       "advice": "registry.yaml 可解析但无 providers——"
                                 "执行 open-free-router setup 生成"})
    else:
        checks.append({"check": "config", "status": "error",
                       "advice": "registry.yaml 不存在或不可解析——"
                                 "执行 open-free-router setup 生成"})
    # 2) keys
    providers = (registry or {}).get("providers") or {}
    for platform, provider in providers.items():
        key_env = provider.get("api_key_env", "")
        if key_env and not env.get(key_env):
            checks.append({"check": "keys", "status": "warning",
                           "advice": f"设置环境变量 {key_env}（BYOS 可后补）"})
            break
    else:
        checks.append({"check": "keys", "status": "ok", "advice": ""})
    # 3) connectivity
    if connectivity_fn is not None:
        all_ok = all(connectivity_fn(p) for p in providers)
        checks.append({"check": "connectivity",
                       "status": "ok" if all_ok else "warning",
                       "advice": "" if all_ok else "至少一个提供方不可达——"
                               "检查网络或提供方状态"})
    # 4) catalog
    if catalog_version:
        status = "ok" if catalog_version >= min_catalog_version else "warning"
        checks.append({"check": "catalog", "status": status,
                       "advice": "" if status == "ok" else
                                 f"目录版本 {catalog_version} 低于底线 "
                                 f"{min_catalog_version}——重新拉取"})
    else:
        checks.append({"check": "catalog", "status": "info",
                       "advice": "签名目录未配置——本地 registry 继续服务"})
    overall = "ok"
    for c in checks:
        if c["status"] == "error":
            overall = "error"
        elif c["status"] == "warning" and overall != "error":
            overall = "warning"
    return {"schema": DOCTOR_SCHEMA, "overall": overall, "checks": checks}


def setup_generate(catalog: dict[str, Any],
                   existing_yaml: dict[str, Any] | None,
                   backup_dir: Path) -> dict[str, Any]:
    """T3 setup 生成器：备份已有配置 → 只填空缺键不覆盖 → 生成草稿。"""
    _require(isinstance(catalog.get("providers"), list), "SETUP_BACKUP_EXISTS",
             "catalog.providers 缺失")
    generated = {}
    for entry in catalog["providers"]:
        platform = entry["platform"]
        if platform in (existing_yaml or {}):
            # 已有键的值（含空值）一律不动
            generated[platform] = existing_yaml[platform]
            continue
        generated[platform] = {
            "upstream_url": f"https://{platform}.example.invalid/v1（占位）",
            "enabled": entry.get("enabled", True),
            "api_key_env": f"{platform.upper().replace('-', '_')}_API_KEY",
            "api_key": "",
            "models": [{"id": m["model_id"],
                        "upstream_id": m.get("upstream_id") or m["model_id"]}
                       for m in entry.get("models", [])]}
    if existing_yaml:
        backup_path = backup_dir / f"registry.yaml.bak-{NOW_PLACEHOLDER}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(json.dumps(existing_yaml, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
    return {"schema": SETUP_SCHEMA, "generated": generated,
            "backup": str(backup_dir) if existing_yaml else None}


NOW_PLACEHOLDER = "2026-11-17T00:00:00Z"


def launch_zero_retention(*, command: list[str], api_keys: dict[str, str],
                          extra_env: dict[str, str] | None = None
                          ) -> dict[str, Any]:
    """T4 零留存启动器：凭据只注入子进程 env，不落盘，不进日志。

    返回子进程 PID 与 env 键名列表（不返回密钥值）。
    调用方负责进程生命周期——本函数只组装 env 并启动。
    """
    _require(bool(command), "LAUNCHER_KEY_NOT_FOUND", "command 不能为空")
    _require(bool(api_keys), "LAUNCHER_KEY_NOT_FOUND", "api_keys 不能为空")
    env = dict(os.environ)
    for name, value in api_keys.items():
        env[name] = value                    # 只注入子进程 env，不写文件
    env.update(extra_env or {})
    proc = subprocess.Popen(command, env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return {"schema": LAUNCHER_SCHEMA, "pid": proc.pid,
            "env_keys_injected": list(api_keys.keys()),   # 只记键名不记值
            "note": "凭据只存在于子进程 env，进程结束即失效——零留存"}
