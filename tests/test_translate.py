"""Translation layer tests (PRD-060/T2): request normalization.

Uses stub targets (duck-typed) so the translation layer is tested in
isolation from routing/registry.
"""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace

import pytest

from open_free_router import translate
from open_free_router.translate import (
    AuthTable,
    RequestTable,
    TranslationTable,
    build_outbound_request,
    canonicalize_request,
    get_table,
    normalize_request,
)


def _provider(name="nous", upstream_url="https://inference-api.nousresearch.com/v1",
              base_url="", auth_mode="bearer"):
    return SimpleNamespace(
        name=name, upstream_url=upstream_url, base_url=base_url,
        auth_mode=auth_mode,
    )


def _target(provider_name="nous", upstream_model_id="nous/hermes-3"):
    provider = _provider(name=provider_name)
    return SimpleNamespace(
        provider=provider, provider_name=provider_name,
        upstream_model_id=upstream_model_id, canonical_id="free-smart",
    )


def _payload(**over):
    p = {
        "model": "free-smart",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "temperature": 0.7,
    }
    p.update(over)
    return p


# --- table lookup -----------------------------------------------------------

def test_get_table_known_provider():
    t = get_table("opencode-zen-free")
    assert t.auth.mode == "none"


def test_get_table_unknown_provider_falls_back_to_default():
    t = get_table("some-future-provider")
    assert t.auth.mode == "bearer"
    assert t.request.model_field == "model"


def test_default_table_for_regular_provider():
    t = get_table("groq")
    assert t.auth.mode == "bearer"


# --- canonicalization (provider-agnostic, was inline in proxy.py) -----------

def test_canonicalize_converts_max_completion_tokens():
    out = canonicalize_request(_payload(max_completion_tokens=50))
    assert out["max_tokens"] == 100  # existing max_tokens wins
    assert "max_completion_tokens" not in out


def test_canonicalize_renames_when_only_alias_present():
    out = canonicalize_request({"model": "m", "max_completion_tokens": 50})
    assert out["max_tokens"] == 50
    assert "max_completion_tokens" not in out


def test_canonicalize_developer_role_to_system():
    out = canonicalize_request(
        {"messages": [{"role": "developer", "content": "x"}]})
    assert out["messages"][0]["role"] == "system"


def test_canonicalize_does_not_mutate_input():
    payload = _payload(max_completion_tokens=50)
    snapshot = copy.deepcopy(payload)
    canonicalize_request(payload)
    assert payload == snapshot


# --- normalize_request: mapping --------------------------------------------

def test_standard_fields_pass_through():
    table = get_table("nous")
    body, dropped = normalize_request(
        table, _payload(), provider_name="nous", request_id="r1")
    assert dropped == []
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.7


def test_message_unknown_field_dropped_with_warning(caplog):
    table = get_table("deepseek")
    payload = {"messages": [
        {"role": "user", "content": "hi", "reasoning_content": "junk"}]}
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        body, dropped = normalize_request(
            table, payload, provider_name="deepseek", request_id="r2")
    assert "reasoning_content" not in body["messages"][0]
    assert dropped == ["messages[].reasoning_content"]
    assert any("deepseek" in r.message and "r2" in r.message
               for r in caplog.records)


def test_undeclared_top_level_field_dropped_with_warning(caplog):
    table = get_table("groq")
    payload = _payload(prompt_cache_key="abc", reasoning_effort="high")
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        body, dropped = normalize_request(
            table, payload, provider_name="groq", request_id="r3")
    assert "prompt_cache_key" not in body
    assert "reasoning_effort" not in body
    assert set(dropped) == {"prompt_cache_key", "reasoning_effort"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # single aggregated warning
    assert "prompt_cache_key" in warnings[0].message


def test_warning_logs_field_names_not_values(caplog):
    table = get_table("groq")
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        normalize_request(table, _payload(secret_sauce="s3cr3t"),
                          provider_name="groq", request_id="r4")
    assert not any("s3cr3t" in r.message for r in caplog.records)


def test_no_warning_when_nothing_dropped(caplog):
    table = get_table("nous")
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        _, dropped = normalize_request(
            table, _payload(), provider_name="nous", request_id="r5")
    assert dropped == []
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_declared_passthrough_survives(caplog):
    table = TranslationTable(
        provider_id="x",
        request=RequestTable(passthrough=("metadata.user_id",)),
    )
    payload = _payload(metadata={"user_id": "u-1", "other": "x"})
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        body, _ = normalize_request(
            table, payload, provider_name="x", request_id="r6")
    assert body["metadata"] == {"user_id": "u-1"}  # only declared path kept
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_input_payload_not_mutated():
    table = get_table("groq")
    payload = _payload(prompt_cache_key="abc",
                       messages=[{"role": "developer", "content": "x",
                                  "reasoning_content": "j"}])
    snapshot = copy.deepcopy(payload)
    normalize_request(table, payload, provider_name="groq", request_id="r7")
    assert payload == snapshot


# --- build_outbound_request --------------------------------------------------

def test_build_sets_model_field_and_headers():
    table = get_table("nous")
    out = build_outbound_request(
        table, target=_target(), payload=_payload(), credential="k",
        endpoint_suffix="chat/completions", request_id="r8")
    assert out.url == ("https://inference-api.nousresearch.com/v1"
                       "/chat/completions")
    assert out.body["model"] == "nous/hermes-3"
    assert out.headers["Authorization"] == "Bearer k"
    assert out.headers["Content-Type"] == "application/json"
    assert "open-free-router" in out.headers["User-Agent"]


def test_build_never_sends_auth_for_keyless_provider():
    table = get_table("opencode-zen-free")
    target = _target(provider_name="opencode-zen-free")
    target.provider = _provider(name="opencode-zen-free",
                                upstream_url="https://opencode.ai/zen/v1",
                                auth_mode="none")
    out = build_outbound_request(
        table, target=target, payload=_payload(), credential="accidental",
        endpoint_suffix="chat/completions", request_id="r9")
    assert "Authorization" not in out.headers


def test_build_no_auth_header_without_credential():
    table = get_table("nous")
    out = build_outbound_request(
        table, target=_target(), payload=_payload(), credential="",
        endpoint_suffix="chat/completions", request_id="r10")
    assert "Authorization" not in out.headers


def test_build_rejects_unconfigured_provider():
    table = get_table("nous")
    target = _target()
    target.provider = _provider(upstream_url="", base_url="")
    with pytest.raises(OSError):
        build_outbound_request(
            table, target=target, payload=_payload(), credential="k",
            endpoint_suffix="chat/completions", request_id="r11")


def test_build_rejects_bad_url():
    table = get_table("nous")
    target = _target()
    target.provider = _provider(upstream_url="ftp://example.com/v1")
    with pytest.raises(OSError):
        build_outbound_request(
            table, target=target, payload=_payload(), credential="k",
            endpoint_suffix="chat/completions", request_id="r12")


def test_build_uses_base_url_fallback():
    table = get_table("nous")
    target = _target()
    target.provider = _provider(upstream_url="", base_url="https://b.example/v1")
    out = build_outbound_request(
        table, target=target, payload=_payload(), credential="k",
        endpoint_suffix="chat/completions", request_id="r13")
    assert out.url == "https://b.example/v1/chat/completions"
