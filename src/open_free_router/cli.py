#!/usr/bin/env python3
"""open-free-router CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from open_free_router import __version__
from open_free_router.config import Config
from open_free_router.registry import Registry, ModelInfo, ProviderConfig
from open_free_router.refresh import refresh
from open_free_router.ui import run_ui
from open_free_router.serve import Daemon
from open_free_router.auth import get_or_create_proxy_token
from open_free_router.discovery import adopt_validated, discover, save_discovery, validate_candidates
from open_free_router.routing import RoutePlanner, diagnose_routing_config


TEMPLATE = Path(__file__).parent / "registry.default.yaml"


def _bootstrap_registry(cfg: Config) -> bool:
    """Copy default template to registry path if no registry exists."""
    if cfg.registry_path.exists():
        return False
    if not TEMPLATE.exists():
        return False
    cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(TEMPLATE, cfg.registry_path)
    return True


def _ensure_config(cfg: Config):
    """Auto-create config.yaml with defaults if none exists."""
    if cfg.path:
        return
    cfg.path = Path.home() / ".config" / "open-free-router" / "config.yaml"
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.path.exists():
        import yaml
        cfg.path.write_text(yaml.dump({
            "proxy": {"host": "127.0.0.1", "port": 8337},
            "ui": {"host": "127.0.0.1", "port": 9057},
        }, default_flow_style=False))


def cmd_refresh(args):
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)

    source = args.source
    results = refresh(reg, provider_name=source)

    if source and source not in results:
        print(f"Unknown source: {source}. Available: {sorted(set(results) | {'openrouter','nvidia-nim'})}")
        sys.exit(1)

    changed = any(v for v in results.values())
    if changed and not args.dry_run:
        reg.save(cfg.registry_path)
        print("\n✔ registry updated")
    elif not changed:
        print("\n✓ no changes")


def cmd_ui(args):
    cfg = Config()
    _bootstrap_registry(cfg)
    run_ui(cfg, port=cfg.ui_port)


def cmd_serve(args):
    cfg = Config()
    _ensure_config(cfg)

    bootstrapped = _bootstrap_registry(cfg)
    if bootstrapped:
        n = len(Registry.load(cfg.registry_path).providers)
        print(f"✔ Created {cfg.registry_path} with {n} providers")
        print("  ⚠ No API keys configured yet.")
        print("  Run:  open-free-router setup")
        print()

    cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
    Daemon(cfg).serve()


def cmd_add(args):
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)

    name = args.name
    base_url = args.base_url
    upstream_url = args.upstream_url or ""
    api_key = args.api_key or ""
    models = [ModelInfo(id=m) for m in (args.models or [])]

    p = ProviderConfig(
        name=name,
        base_url=base_url,
        upstream_url=upstream_url,
        api_key=api_key,
        models=models,
        auto_refresh=args.auto_refresh,
        refresh_method="api" if args.auto_refresh else "manual",
    )
    reg.add_provider(p)
    reg.save(cfg.registry_path)
    print(f"✔ Added provider '{name}' with {len(models)} models")


def cmd_setup(args):
    """Interactive wizard: fill in missing API keys."""
    cfg = Config()
    _ensure_config(cfg)
    bootstrapped = _bootstrap_registry(cfg)

    reg = Registry.load(cfg.registry_path)
    if not reg.providers:
        print("✗ No providers in registry.")
        return

    changed = False
    for name, p in reg.providers.items():

        if p.auth_mode == "none":
            print(f"  {name:25s} ✓ keyless endpoint")
            continue

        if p.api_key_env:
            status = "✓ available" if p.effective_key else "✗ not set"
            print(f"  {name:25s} {status} via ${p.api_key_env}")
            continue

        if p.api_key:
            masked = f"{p.api_key[:8]}...{p.api_key[-4:]}" if len(p.api_key) > 12 else "***"
            print(f"  {name:25s} ✓ {masked}")
            continue

        print(f"  {name:25s} ✗ no key")
        print(f"    upstream: {p.upstream_url or p.base_url}")
        try:
            val = input(f"    Enter API key for {name} (leave blank to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if val:
            p.api_key = val
            changed = True
            print(f"    ✓ key saved")

    if changed:
        reg.save(cfg.registry_path)
        n = sum(1 for p in reg.providers.values() if p.api_key)
        print(f"\n✔ Saved {cfg.registry_path} — {n}/{len(reg.providers)} providers have keys")
        print("  Run  open-free-router serve  to start.")
    else:
        print("\n✓ No changes.")


def cmd_sync(args):
    """Sync registry to agent configs (Pi, OMP, OpenCode, Hermes, Codex,
    Claude Code, Kimi CLI, OpenClaw, WorkBuddy)."""
    from open_free_router.sync import sync_all
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)
    proxy_token = get_or_create_proxy_token(cfg.config_dir)

    agents = None
    if args.agent:
        agents = [a.strip() for a in args.agent.split(",")]

    do_write = not args.diff
    results = sync_all(
        reg,
        do_write=do_write,
        agents=agents,
        proxy_url=f"http://{cfg.proxy_host}:{cfg.proxy_port}/v1",
        proxy_token=proxy_token,
        codex_model=args.codex_model or cfg.codex_model,
        claude_model=args.claude_model or cfg.claude_model,
    )

    label = "DIFF" if args.diff else "SYNC"
    print(f"\n=== {label} ===")
    for agent, changes in results.items():
        status = "✔" if do_write else "?"
        print(f"  {status} {agent}: {changes}")

    if do_write:
        print("\n✔ Agent configurations synchronized")
        if agents and "codex" in agents:
            print("  Start Codex with: codex --profile open-free-router")
        if agents and "claude" in agents:
            print("  Claude Code now routes through the local proxy (~/.claude/settings.json env)")
        if not agents or set(agents) - {"codex", "claude"}:
            print("  Restart agents to apply: opencode, hermes, kimi, openclaw, workbuddy")


def cmd_token(args):
    """Print only the local proxy token for command-backed clients."""
    cfg = Config()
    _ensure_config(cfg)
    print(get_or_create_proxy_token(cfg.config_dir))


def cmd_mcp(args):
    """Run the MCP stdio server (or print host registration snippets)."""
    from open_free_router.mcp_server import main as mcp_main
    if not args.print_config:
        _bootstrap_registry(Config())
    mcp_main(print_config=args.print_config)


def _status_payload(cfg: Config) -> dict:
    from open_free_router.mcp_server import McpServer
    server = McpServer(cfg)
    result = server.tool_get_status({})
    import json as _json
    return _json.loads(result["content"][0]["text"])


def cmd_status(args):
    """One-shot health summary; --json for scripting."""
    cfg = Config()
    _bootstrap_registry(cfg)
    payload = _status_payload(cfg)
    if args.json:
        import json as _json
        print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"open-free-router {payload['version']}")
    print(f"  config   : {payload['config_path'] or '(defaults)'}")
    print(f"  registry : {payload['registry_path']}")
    print(f"  providers: {payload['providers']} ({payload['providers_with_key']} with keys)")
    print(f"  models   : {payload['models']}")
    proxy_state = "up" if payload["proxy_reachable"] else "down"
    ui_state = "up" if payload["ui_reachable"] else "down"
    print(f"  proxy    : {payload['proxy_url']} [{proxy_state}]")
    print(f"  ui       : {payload['ui_url']} [{ui_state}]")


def cmd_models(args):
    """List registry models; --json for scripting."""
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)
    rows = []
    for name, p in reg.providers.items():
        for m in p.models:
            free_tier = p.free_tier_for(m).to_dict(include_status=True)
            rows.append({
                "id": f"{p.model_prefix}/{m.id}",
                "name": m.name or m.id,
                "provider": name,
                "context_window": m.context_window,
                "max_tokens": m.max_tokens,
                "reasoning": m.reasoning,
                "tool_calling": m.tool_calling,
                "free_tier": free_tier,
            })
    planner = RoutePlanner(reg, cfg.routing)
    for model_id in planner.virtual_model_ids():
        plan = planner.plan(model_id)
        rows.append({
            "id": model_id,
            "name": model_id,
            "provider": "open-free-router",
            "context_window": 0,
            "max_tokens": 0,
            "reasoning": any(target.model.reasoning for target in plan.candidates),
            "tool_calling": bool(plan.candidates) and all(
                target.model.tool_calling for target in plan.candidates
            ),
            "virtual": True,
            "candidate_count": len(plan.candidates),
            "free_tier": {
                "status": (
                    "verified" if plan.candidates and all(
                        target.provider.free_tier_for(target.model).status() == "verified"
                        for target in plan.candidates
                    ) else "mixed_or_unknown"
                )
            },
        })
    if args.json:
        import json as _json
        print(_json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for row in rows:
        flags = "".join([
            "T" if row["tool_calling"] else "-",
            "R" if row["reasoning"] else "-",
        ])
        free_status = row.get("free_tier", {}).get("status", "unknown")
        print(f"  {row['id']:42s} {flags}  {row['context_window']:>9,}ctx  {row['provider']}  free={free_status}")
    print(f"\n{len(rows)} models (T=tool calling, R=reasoning)")


def cmd_route_explain(args):
    """Explain deterministic route planning without calling an upstream."""
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)
    explanation = RoutePlanner(reg, cfg.routing).plan(args.model).explain()
    if args.json:
        import json as _json
        print(_json.dumps(explanation, indent=2, ensure_ascii=False))
        return
    print(f"requested: {explanation['requested_model']}")
    print(f"strategy : {explanation['strategy']}")
    print(f"selected : {explanation['selected'] or '(none)'}")
    print("candidates:")
    for index, model_id in enumerate(explanation["candidates"], 1):
        print(f"  {index}. {model_id}")
    if explanation.get("scoring", {}).get("enabled"):
        print("scores:")
        for candidate in explanation["scoring"]["candidates"]:
            factors = ", ".join(
                f"{name}={item['value']:.2f}×{item['weight']:.2f}"
                for name, item in candidate["factors"].items()
            )
            print(f"  {candidate['model']}: {candidate['total']:.3f} ({factors})")
    if explanation["rejected"]:
        print("rejected:")
        for item in explanation["rejected"]:
            print(f"  - {item['model']}: {item['reason']}")


def _resilience_request(cfg: Config, method: str = "GET", payload: dict | None = None) -> dict:
    import json as _json
    from urllib.request import Request, urlopen

    token = get_or_create_proxy_token(cfg.config_dir)
    url = f"http://{cfg.proxy_host}:{cfg.proxy_port}/api/resilience"
    if method == "POST":
        url += "/reset"
    data = _json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=5) as response:
        return _json.loads(response.read())


def cmd_resilience(args):
    """Read the running proxy's redacted resilience state."""
    cfg = Config()
    try:
        payload = _resilience_request(cfg)
    except Exception as exc:
        print(f"✗ cannot read proxy resilience state: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        import json as _json
        print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print("provider circuits:")
    for name, state in payload.get("providers", {}).items():
        print(f"  {name:24s} {state.get('state', 'unknown'):10s} failures={state.get('failures', 0)}")
    print("credential states:")
    for name, state in payload.get("credentials", {}).items():
        quota = state.get("quota") or {}
        requests = quota.get("requests") or {}
        tokens = quota.get("tokens") or {}
        details = []
        if requests.get("remaining") is not None:
            details.append(f"requests={requests['remaining']}/{requests.get('limit', '?')}")
        if tokens.get("remaining") is not None:
            details.append(f"tokens={tokens['remaining']}/{tokens.get('limit', '?')}")
        suffix = " ".join(filter(None, [state.get("reason", ""), *details]))
        print(f"  {name:24s} {state.get('state', 'unknown'):10s} {suffix}")
    print("model lockouts:")
    for name, state in payload.get("models", {}).items():
        print(f"  {name:42s} {state.get('reason', '')}")


def cmd_resilience_reset(args):
    """Precisely reset one provider or provider/model runtime state."""
    cfg = Config()
    try:
        payload = _resilience_request(
            cfg, method="POST", payload={"provider": args.provider, "model": args.model}
        )
    except Exception as exc:
        print(f"✗ cannot reset proxy resilience state: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"✔ reset {payload['provider']}" + (f"/{payload['model']}" if payload.get("model") else ""))


def _metrics_request(cfg: Config, days: int, export_format: str = "") -> tuple[bytes, str]:
    from urllib.request import Request, urlopen

    host = "127.0.0.1" if cfg.proxy_host in ("0.0.0.0", "::") else cfg.proxy_host
    path = "/api/metrics"
    if export_format:
        path += "/export"
    query = f"?days={max(1, int(days))}"
    if export_format:
        query += f"&format={export_format}"
    request = Request(
        f"http://{host}:{cfg.proxy_port}{path}{query}",
        headers={"Authorization": f"Bearer {get_or_create_proxy_token(cfg.config_dir)}"},
    )
    with urlopen(request, timeout=10) as response:
        return response.read(), response.getheader("Content-Type", "application/json")


def cmd_metrics(args):
    """Read or export privacy-minimized local usage analytics."""
    cfg = Config()
    try:
        raw, _ = _metrics_request(cfg, args.days, args.export or "")
    except Exception as exc:
        print(f"✗ cannot read local usage analytics: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.export:
        text = raw.decode("utf-8")
        if args.output:
            output_path = Path(args.output).expanduser()
            output_path.write_text(text, encoding="utf-8")
            output_path.chmod(0o600)
            print(f"✔ exported redacted analytics to {args.output}")
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        return
    import json as _json
    payload = _json.loads(raw)
    if args.json:
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not payload.get("enabled"):
        print("本地使用分析已关闭（analytics.retention_days: 0）")
        return
    overall = payload.get("overall", {})
    print(f"近 {payload.get('period_days', args.days)} 天本机使用分析")
    print(f"  请求: {overall.get('requests', 0)}  成功率: {overall.get('success_rate', 0) * 100:.1f}%")
    print(f"  Fallback: {overall.get('fallback_rate', 0) * 100:.1f}%  挽回前序失败: {overall.get('saved_failed_requests', 0)}")
    print(f"  p50/p95 总耗时: {overall.get('total_p50_ms')} / {overall.get('total_p95_ms')} ms")
    print(f"  Token: 输入 {overall.get('input_tokens', 0)} / 输出 {overall.get('output_tokens', 0)}")
    print(f"  说明: {payload.get('quota_estimate_notice_zh', '')}")


def cmd_protocols(args):
    """Print the declared provider × client-protocol capability matrix."""
    import json as _json
    from open_free_router.protocol_matrix import protocol_matrix

    cfg = Config()
    _bootstrap_registry(cfg)
    matrix = protocol_matrix(Registry.load(cfg.registry_path))
    if args.json:
        print(_json.dumps(matrix, ensure_ascii=False, indent=2))
        return
    print(
        f"协议兼容矩阵：{matrix['provider_count']} 个提供商 × "
        f"{matrix['protocol_count']} 个客户端协议"
    )
    for row in matrix["rows"]:
        state = "已配置" if row["configured"] else "需修复"
        key = "凭据就绪" if row["credential_ready"] else "缺少凭据"
        tools = row["tool_calling_models"]
        print(
            f"  {row['provider']:24s} {row['protocol']:20s} "
            f"{state:6s} {key:8s} 模型={row['model_count']} 工具模型={tools}"
        )
    for issue in matrix["issues"]:
        print(f"  ✗ {issue['provider']}: {issue['message']}")
        print(f"      fix: {issue['fix']}")
    print(f"\n说明：{matrix['notice_zh']}")


def cmd_doctor(args):
    """Diagnose the local install: config, registry, ports, client configs."""
    import json as _json
    from open_free_router import sync as sync_mod
    cfg = Config()
    payload = _status_payload(cfg)
    failures = 0
    json_mode = bool(getattr(args, "json", False))
    checks: list[dict] = []

    def check(ok: bool, label: str, detail: str = "", warn_only: bool = False):
        nonlocal failures
        mark = "✓" if ok else ("⚠" if warn_only else "✗")
        if not ok and not warn_only:
            failures += 1
        suffix = f" — {detail}" if detail else ""
        checks.append({
            "ok": ok,
            "severity": "warning" if warn_only and not ok else ("error" if not ok else "ok"),
            "label": label,
            "detail": detail,
        })
        if not json_mode:
            print(f"  {mark} {label}{suffix}")

    if not json_mode:
        print("open-free-router doctor")
    check(cfg.path is not None and cfg.path.exists(), "config.yaml found",
          str(cfg.path) if cfg.path else "run `open-free-router serve` once to create it", warn_only=True)
    check(cfg.registry_path.exists(), "registry.yaml found", str(cfg.registry_path))
    check(payload["providers"] > 0, "providers registered", f"{payload['providers']}")
    check(payload["providers_with_key"] > 0, "at least one provider has an API key",
          f"{payload['providers_with_key']}/{payload['providers']} — run `open-free-router setup`")
    check(payload["models"] > 0, "models in registry", f"{payload['models']}")
    check(payload["proxy_reachable"], "proxy reachable", payload["proxy_url"]
          + ("" if payload["proxy_reachable"] else " — run `open-free-router serve`"), warn_only=True)
    check(payload["ui_reachable"], "dashboard reachable", payload["ui_url"], warn_only=True)

    registry = Registry.load(cfg.registry_path) if cfg.registry_path.exists() else Registry({})
    from open_free_router.protocol_matrix import protocol_matrix
    compatibility = protocol_matrix(registry)
    failures += sum(issue["severity"] == "error" for issue in compatibility["issues"])
    if not json_mode:
        print("\nprotocol compatibility:")
        print(
            f"  · {compatibility['provider_count']} providers × "
            f"{compatibility['protocol_count']} client protocols"
        )
        if not compatibility["issues"]:
            print("  ✓ declared upstream protocol configuration is valid")
        for issue in compatibility["issues"]:
            mark = "✗" if issue["severity"] == "error" else "⚠"
            print(f"  {mark} {issue['provider']}: {issue['message']}")
            print(f"      fix: {issue['fix']}")
    routing_issues = diagnose_routing_config(cfg._raw.get("routing"), registry)
    failures += sum(issue.severity == "error" for issue in routing_issues)
    if not json_mode:
        print("\nrouting config:")
        if not routing_issues:
            print("  ✓ deterministic routing configuration is valid")
        for issue in routing_issues:
            mark = "✗" if issue.severity == "error" else "⚠"
            print(f"  {mark} {issue.path}: {issue.message}")
            if issue.fix:
                print(f"      fix: {issue.fix}")

    from open_free_router.evidence import summarize_evidence
    effective_evidence = [
        provider.free_tier_for(model)
        for provider in registry.providers.values() for model in provider.models
    ]
    evidence_summary = summarize_evidence(effective_evidence)
    evidence_issues = []
    for provider_name, provider in registry.providers.items():
        configured = [(f"providers.{provider_name}.free_tier", provider.free_tier)]
        configured.extend(
            (f"providers.{provider_name}.models.{model.id}.free_tier", model.free_tier)
            for model in provider.models if model.free_tier.configured
        )
        for path, evidence in configured:
            if not evidence.configured:
                continue
            state = evidence.status()
            if state == "invalid":
                evidence_issues.append({
                    "severity": "error", "code": "invalid_free_tier_evidence", "path": path,
                    "message": "; ".join(evidence.validation_errors),
                    "fix": "修正该 free_tier 字段后运行 `open-free-router doctor` 复查",
                })
            elif state in ("expired", "unverified"):
                evidence_issues.append({
                    "severity": "warning", "code": f"free_tier_{state}", "path": path,
                    "message": "免费条件证据已过期，必须重新核验。" if state == "expired" else "免费条件缺少有效来源或核验期限。",
                    "fix": "更新 evidence_url、verified_at 和 expires_at 后运行 `open-free-router doctor` 复查",
                })
    failures += sum(issue["severity"] == "error" for issue in evidence_issues)
    if not json_mode:
        print("\nfree-tier evidence:")
        print(
            "  · model evidence "
            + ", ".join(f"{name}={count}" for name, count in evidence_summary.items())
        )
        for issue in evidence_issues:
            mark = "✗" if issue["severity"] == "error" else "⚠"
            print(f"  {mark} {issue['path']}: {issue['message']}")
            print(f"      fix: {issue['fix']}")

    if not json_mode:
        print("\nclient configs:")
    clients = [
        ("Codex profile", sync_mod.CODEX_PROFILE),
        ("Claude Code settings", sync_mod.CLAUDE_SETTINGS),
        ("OpenCode", sync_mod.OPENCODE_CONFIG),
        ("Hermes", sync_mod.HERMES_CONFIG),
        ("Kimi CLI", sync_mod.KIMI_CONFIG),
        ("OpenClaw", sync_mod.OPENCLAW_CONFIG),
        ("WorkBuddy", sync_mod.WORKBUDDY_MODELS),
        ("Pi", sync_mod.PI_MODELS_PATH),
        ("OMP", sync_mod.OMP_MODELS),
    ]
    for label, path in clients:
        mark = "✓" if path.exists() else "·"
        if not json_mode:
            print(f"  {mark} {label:22s} {path}")

    report = {
        "ok": failures == 0,
        "problem_count": failures,
        "checks": checks,
        "routing": [issue.to_dict() for issue in routing_issues],
        "free_tier": {"summary": evidence_summary, "issues": evidence_issues},
        "protocol_matrix": compatibility,
        "clients": [
            {"name": label, "configured": path.exists(), "path": str(path)}
            for label, path in clients
        ],
    }
    if json_mode:
        print(_json.dumps(report, indent=2, ensure_ascii=False))

    if failures:
        if not json_mode:
            print(f"\n✗ {failures} problem(s) found")
        sys.exit(1)
    if not json_mode:
        print("\n✔ no critical problems found")


def cmd_discover(args):
    """Search public metadata feeds for review-only provider candidates."""
    cfg = Config()
    _bootstrap_registry(cfg)
    reg = Registry.load(cfg.registry_path)
    snapshot = discover(reg, timeout=args.timeout)
    if args.test or args.adopt:
        validate_candidates(
            snapshot,
            timeout=args.timeout,
            max_providers=args.max_providers,
            max_models=args.max_models,
        )
    print(
        f"Found {snapshot['candidate_provider_count']} candidate providers and "
        f"{snapshot['candidate_model_count']} explicit-free models."
    )
    for provider in snapshot["providers"][:20]:
        state = provider.get("validation", {}).get("state", "candidate")
        print(f"  {provider['id']:24s} {provider['model_count']:3d} models  {state:18s} {provider['api']}")
    if args.dry_run:
        print("Dry run: registry and discovery snapshot were not changed.")
        return
    output = Path(args.output).expanduser() if args.output else cfg.discovery_path
    if args.adopt:
        adopted = adopt_validated(snapshot, reg)
        if adopted:
            reg.save(cfg.registry_path)
        print(f"Auto-adopted {len(adopted)} verified providers: {', '.join(adopted) or 'none'}")
    save_discovery(snapshot, output)
    print(f"Saved review-only candidates to {output}")


def main():
    parser = argparse.ArgumentParser(
        prog="open-free-router",
        description="Free LLM model router & sync engine",
    )
    # The npm install guide tells users to verify the install with
    # `open-free-router --version`; without this it exits 2 on a fresh setup.
    parser.add_argument(
        "--version", action="version", version=f"open-free-router {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    p_refresh = sub.add_parser("refresh", help="refresh free model lists from APIs")
    p_refresh.add_argument("--source", help="only refresh this source")
    p_refresh.add_argument("--dry-run", action="store_true")
    p_refresh.set_defaults(func=cmd_refresh)

    p_ui = sub.add_parser("ui", help="start web dashboard")
    p_ui.set_defaults(func=cmd_ui)

    p_serve = sub.add_parser("serve", help="start all services: proxy + UI + scheduler")
    p_serve.set_defaults(func=cmd_serve)

    p_add = sub.add_parser("add", help="add a new provider")
    p_add.add_argument("name")
    p_add.add_argument("--base-url", required=True)
    p_add.add_argument("--upstream-url", default="", help="override upstream URL (defaults to --base-url)")
    p_add.add_argument("--api-key", default="")
    p_add.add_argument("--model", action="append", dest="models")
    p_add.add_argument("--auto-refresh", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_setup = sub.add_parser("setup", help="interactive wizard: configure API keys for all providers")
    p_setup.set_defaults(func=cmd_setup)

    p_sync = sub.add_parser("sync", help="sync registry to agent configs")
    p_sync.add_argument(
        "--agent",
        help="comma-separated agent names: pi,omp,opencode,hermes,codex,claude,kimi,openclaw,workbuddy",
    )
    p_sync.add_argument("--diff", action="store_true", help="show diff only, don't write")
    p_sync.add_argument("--codex-model", help="registry model ID for the Codex profile")
    p_sync.add_argument("--claude-model", help="registry model ID for Claude Code's main model")
    p_sync.set_defaults(func=cmd_sync)

    p_token = sub.add_parser("token", help="print the local inference proxy token")
    p_token.set_defaults(func=cmd_token)

    p_mcp = sub.add_parser("mcp", help="run the MCP stdio server for agent hosts")
    p_mcp.add_argument("--print-config", action="store_true",
                       help="print MCP registration snippets instead of serving")
    p_mcp.set_defaults(func=cmd_mcp)

    p_status = sub.add_parser("status", help="one-shot health summary")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_models = sub.add_parser("models", help="list registry models")
    p_models.add_argument("--json", action="store_true")
    p_models.set_defaults(func=cmd_models)

    p_route = sub.add_parser("route", help="inspect deterministic virtual-model routing")
    route_sub = p_route.add_subparsers(dest="route_command", required=True)
    p_route_explain = route_sub.add_parser("explain", help="explain a model route without inference")
    p_route_explain.add_argument("model")
    p_route_explain.add_argument("--json", action="store_true")
    p_route_explain.set_defaults(func=cmd_route_explain)

    p_resilience = sub.add_parser("resilience", help="inspect or reset runtime failure isolation")
    p_resilience.add_argument("--json", action="store_true")
    p_resilience.set_defaults(func=cmd_resilience)
    resilience_sub = p_resilience.add_subparsers(dest="resilience_command")
    p_resilience_reset = resilience_sub.add_parser("reset", help="reset one provider or model state")
    p_resilience_reset.add_argument("--provider", required=True)
    p_resilience_reset.add_argument("--model")
    p_resilience_reset.set_defaults(func=cmd_resilience_reset)

    p_metrics = sub.add_parser("metrics", help="inspect or export local privacy-minimized analytics")
    p_metrics.add_argument("--days", type=int, default=30)
    p_metrics.add_argument("--json", action="store_true")
    p_metrics.add_argument("--export", choices=("json", "csv"))
    p_metrics.add_argument("--output", help="write export to a local file")
    p_metrics.set_defaults(func=cmd_metrics)

    p_protocols = sub.add_parser("protocols", help="show provider × client-protocol compatibility")
    p_protocols.add_argument("--json", action="store_true")
    p_protocols.set_defaults(func=cmd_protocols)

    p_doctor = sub.add_parser("doctor", help="diagnose the local install")
    p_doctor.add_argument("--json", action="store_true", help="print structured diagnostics")
    p_doctor.set_defaults(func=cmd_doctor)

    p_discover = sub.add_parser("discover", help="find review-only candidate free-model providers")
    p_discover.add_argument("--dry-run", action="store_true")
    p_discover.add_argument("--output", help="override discovery snapshot path")
    p_discover.add_argument("--timeout", type=int, default=30)
    p_discover.add_argument("--test", action="store_true", help="test candidates using declared credential env vars")
    p_discover.add_argument("--adopt", action="store_true", help="add only models that pass authenticated testing")
    p_discover.add_argument("--max-providers", type=int, default=5)
    p_discover.add_argument("--max-models", type=int, default=3)
    p_discover.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except BrokenPipeError:
        # stdout piped into a pager/head that exited; not an error
        import os
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
