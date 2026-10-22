from open_free_router.quota_view import quota_view, public_quota_bands
from open_free_router.resilience import ResilienceManager
from open_free_router.quota import parse_quota_headers


def test_panel_uses_only_observed_values_and_preserves_units():
    manager = ResilienceManager()
    manager.record_quota('synthetic', 0, parse_quota_headers({
        'x-ratelimit-limit-requests': '100', 'x-ratelimit-remaining-requests': '5',
        'x-ratelimit-reset-requests': '60s', 'x-ratelimit-limit-tokens': '1000',
    }, 200, now=1000))
    view = quota_view(manager.snapshot(), ['synthetic', 'unknown'], now=1001)
    row = view['items'][0]
    assert row['band'] == 'tight'
    assert row['source'] == 'upstream_headers'
    assert row['observed_at'] == 1000
    assert row['valid_until'] == 2800
    assert row['dimensions']['requests'] == {'limit': 100, 'used': None, 'remaining': 5, 'reset_at': 1060}
    assert row['dimensions']['tokens']['remaining'] is None
    assert view['items'][1]['band'] == 'unknown'
    for now in (999, 2801):
        assert quota_view(manager.snapshot(), now=now)['items'][0]['band'] == 'stale'
    assert quota_view(manager.snapshot(), now=2800)['items'][0]['band'] == 'tight'
    assert public_quota_bands(view) == [{'provider': 'synthetic', 'band': 'tight'}, {'provider': 'unknown', 'band': 'unknown'}]


def test_zero_is_exhausted_and_public_projection_excludes_slot_details():
    manager = ResilienceManager()
    manager.record_quota('p', 0, parse_quota_headers({'x-ratelimit-limit-tokens': '100', 'x-ratelimit-remaining-tokens': '0'}, 200, now=1000))
    view = quota_view(manager.snapshot(), now=1001)
    assert view['items'][0]['band'] == 'exhausted'
    assert public_quota_bands(view) == [{'provider': 'p', 'band': 'exhausted'}]


def test_quota_wording_cannot_override_numeric_observation():
    manager = ResilienceManager()
    manager.record_quota('p', 0, parse_quota_headers({
        'x-ratelimit-limit-requests': '100', 'x-ratelimit-remaining-requests': '80',
    }, 429, 'quota_exhausted', now=1000))
    row = quota_view(manager.snapshot(), now=1001)['items'][0]
    assert row['band'] == 'sufficient'
    assert row['dimensions']['requests']['remaining'] == 80
    assert row['source'] == 'upstream_headers'
    assert row['event_classification'] == 'quota_exhausted'
    assert row['classification_source'] == 'failure_classification_hint'
    assert row['classification_conflict'] is True


def test_quota_wording_without_numbers_leaves_balance_unknown():
    manager = ResilienceManager()
    manager.record_quota('p', 0, parse_quota_headers({}, 429, 'quota_exhausted', now=1000))
    row = quota_view(manager.snapshot(), now=1001)['items'][0]
    assert row['band'] == 'unknown'
    assert row['source'] == 'event'
    assert row['event_classification'] == 'quota_exhausted'
    assert row['dimensions']['requests']['remaining'] is None
