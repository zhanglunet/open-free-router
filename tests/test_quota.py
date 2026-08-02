import json
from email.utils import formatdate

from open_free_router.quota import parse_quota_headers


def test_parses_standard_and_provider_specific_dimensions():
    observation = parse_quota_headers({
        "RateLimit-Limit": "100",
        "RateLimit-Remaining": "42",
        "RateLimit-Reset": "60s",
        "X-RateLimit-Limit-Tokens": "20000",
        "X-RateLimit-Remaining-Tokens": "12345",
        "X-RateLimit-Reset-Tokens": "2m",
    }, 200, now=1000)
    assert observation.classification == "available"
    assert observation.requests.limit == 100
    assert observation.requests.remaining == 42
    assert observation.requests.reset_at == 1060
    assert observation.tokens.remaining == 12345
    assert observation.tokens.reset_at == 1120
    assert observation.next_reset_at == 1060


def test_retry_and_long_quota_windows_are_normalized():
    observation = parse_quota_headers({
        "Retry-After": formatdate(1120, usegmt=True),
        "X-RateLimit-Reset-Requests": "1d",
        "X-RateLimit-Reset-Tokens": "2w",
    }, 429, "quota_exhausted", now=1000)
    assert observation.classification == "quota_exhausted"
    assert observation.retry_at == 1120
    assert observation.requests.reset_at == 87400
    assert observation.tokens.reset_at == 1210600
    assert observation.next_reset_at == 1120


def test_credit_exhaustion_and_malformed_headers_are_safe():
    observation = parse_quota_headers({
        "Authorization": "Bearer super-secret",
        "X-RateLimit-Limit": "NaN",
        "X-RateLimit-Remaining": "secret-value",
    }, 402, now=1000)
    encoded = json.dumps(observation.to_dict())
    assert observation.classification == "credits_exhausted"
    assert observation.has_headers is False
    assert "super-secret" not in encoded
    assert "authorization" not in encoded.lower()
