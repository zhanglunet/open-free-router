#!/usr/bin/env python3
"""open-free-router daemon — proxy + UI + scheduler in one process."""
from __future__ import annotations

import json
import time
import threading
import signal
import sys
from pathlib import Path

from open_free_router.config import Config
from open_free_router.registry import Registry
from open_free_router.proxy import run_proxy, rebuild_proxy_index
from open_free_router.ui import run_ui
from open_free_router.refresh import refresh
from open_free_router.sync import write_pi_models, sync_all
from open_free_router.auth import get_or_create_proxy_token
from open_free_router.resilience import ResilienceManager
from open_free_router.telemetry import RouteDecisionStore
from open_free_router.discovery import adopt_validated, discover, save_discovery, validate_candidates
from open_free_router.analytics import AnalyticsStore


class Daemon:
    """Runs proxy + UI + scheduler in one process."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.reg = Registry.load(cfg.registry_path)
        self.proxy_token = get_or_create_proxy_token(cfg.config_dir)
        self.resilience = ResilienceManager(state_path=cfg.data_dir / "runtime-state.json")
        self.decisions = RouteDecisionStore()
        self.analytics = AnalyticsStore(
            cfg.analytics_path, retention_days=cfg.analytics_retention_days
        )
        self.decisions.analytics = self.analytics
        self._proxy_server = None
        self._stop = threading.Event()

    def _scheduler(self):
        interval_hours = self.cfg.refresh_interval_hours
        while not self._stop.wait(interval_hours * 3600):
            print(f"[scheduler] refreshing free models (every {interval_hours}h)...")
            # Never probe on the schedule: validating candidates spends real
            # free-tier quota every cycle and can trip the rate limits it is
            # meant to detect. Operators opt in with `refresh --probe-new`.
            results = refresh(self.reg, probe_new=False)
            if any(results.values()):
                self.reg.save(self.cfg.registry_path)
                rebuild_proxy_index()
                print("[scheduler] registry updated")
            proxy_url = f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1"
            write_pi_models(self.reg, proxy_url=proxy_url)
            sync_all(self.reg, proxy_url=proxy_url, proxy_token=self.proxy_token, exclude=self.cfg.sync_exclude)

    def _discovery_scheduler(self):
        if not self.cfg.discovery_enabled:
            return
        interval_hours = self.cfg.discovery_interval_hours
        while not self._stop.is_set():
            try:
                snapshot = discover(self.reg)
                if self.cfg.discovery_auto_test or self.cfg.discovery_auto_adopt:
                    validate_candidates(
                        snapshot,
                        max_providers=self.cfg.discovery_max_providers,
                        max_models=self.cfg.discovery_max_models,
                    )
                if self.cfg.discovery_auto_adopt:
                    adopted = adopt_validated(snapshot, self.reg)
                    if adopted:
                        self.reg.save(self.cfg.registry_path)
                        rebuild_proxy_index()
                        proxy_url = f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1"
                        sync_all(self.reg, proxy_url=proxy_url, proxy_token=self.proxy_token, exclude=self.cfg.sync_exclude)
                        print(f"[discovery] auto-adopted: {', '.join(adopted)}")
                save_discovery(snapshot, self.cfg.discovery_path)
                print(
                    "[discovery] found "
                    f"{snapshot['candidate_provider_count']} provider candidates / "
                    f"{snapshot['candidate_model_count']} explicit-free models"
                )
            except Exception as exc:
                print(f"[discovery] scan failed: {exc}")
            if self._stop.wait(interval_hours * 3600):
                break

    def serve(self):
        print(f"  Proxy  : {self.cfg.proxy_host}:{self.cfg.proxy_port}")
        print(f"  UI     : http://{self.cfg.ui_host}:{self.cfg.ui_port}")
        print(f"  Refresh: every {self.cfg.refresh_interval_hours}h")
        print(
            "  Discover: "
            + (f"every {self.cfg.discovery_interval_hours}h" if self.cfg.discovery_enabled else "disabled")
        )
        print(f"  Timeout: {self.cfg.upstream_timeout}s")
        print(f"  Auth   : {self.cfg.config_dir / 'proxy.token'}")
        print(
            "  Analytics: "
            + (f"local {self.cfg.analytics_retention_days}d" if self.analytics.enabled else "disabled")
        )
        print()

        # Start proxy
        srv, _ = run_proxy(self.reg, host=self.cfg.proxy_host, port=self.cfg.proxy_port,
                           upstream_timeout=self.cfg.upstream_timeout,
                           auth_token=self.proxy_token,
                           routing=self.cfg.routing,
                           resilience=self.resilience,
                           decisions=self.decisions,
                           analytics=self.analytics)
        self._proxy_server = srv

        # Write Pi models on startup
        proxy_url = f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1"
        write_pi_models(self.reg, proxy_url=proxy_url)
        sync_all(self.reg, proxy_url=proxy_url, proxy_token=self.proxy_token, exclude=self.cfg.sync_exclude)

        threads = [
            threading.Thread(target=run_ui, args=(self.cfg, self.cfg.ui_port, self.reg), daemon=True),
            threading.Thread(target=self._scheduler, daemon=True),
            threading.Thread(target=self._discovery_scheduler, daemon=True),
        ]

        for t in threads:
            t.start()

        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        except KeyboardInterrupt:
            self._stop.set()

        print("\nShutting down...")
        if self._proxy_server:
            self._proxy_server.shutdown()
        self.analytics.close()
        for t in threads:
            t.join(timeout=5)
        print("Done.")


def main():
    cfg = Config()
    Daemon(cfg).serve()
