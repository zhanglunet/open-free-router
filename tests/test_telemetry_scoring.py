from open_free_router.telemetry import RouteDecisionStore


def test_route_history_aggregates_success_and_p95_latencies():
    store = RouteDecisionStore(max_entries=10)
    first = store.record(
        request_id="one", requested_model="auto", strategy="scored", status="success",
        attempts=1, rejected=[], provider="p", model="p/model", status_code=200,
        ttfb_ms=100,
    )
    store.complete(first["request_id"], 300)
    store.record(
        request_id="two", requested_model="auto", strategy="scored", status="failed",
        attempts=1, rejected=[], provider="p", model="p/model", status_code=503,
        ttfb_ms=500, total_latency_ms=700,
    )
    signals = store.scoring_signals()["p/model"]
    assert signals == {
        "success_rate": 0.5,
        "ttfb_p95_ms": 500.0,
        "total_p95_ms": 700.0,
    }


def test_route_history_score_shape_is_bounded_and_redacted():
    class Score:
        def to_dict(self):
            return {
                "model": "p/model",
                "total": 0.8,
                "factors": {"health": {
                    "value": 1, "weight": 0.25, "contribution": 0.25,
                    "source": "runtime",
                }},
            }

    store = RouteDecisionStore()
    item = store.record(
        request_id="id", requested_model="auto", strategy="scored", status="success",
        attempts=1, rejected=[], scores=[Score()],
    )
    assert item["scores"][0]["total"] == 0.8
    assert item["scores"][0]["factors"]["health"]["source"] == "runtime"
    assert "api_key" not in str(item).lower()


def test_fallback_attempts_feed_each_models_success_rate():
    store = RouteDecisionStore()
    store.record(
        request_id="fallback", requested_model="auto", strategy="scored",
        status="success", attempts=2, rejected=[], provider="second",
        model="b/model", status_code=200,
        attempt_results=[
            {"provider": "first", "model": "a/model", "status": "failed",
             "status_code": 429, "ttfb_ms": 400, "total_latency_ms": 500},
            {"provider": "second", "model": "b/model", "status": "success",
             "status_code": 200, "ttfb_ms": 100, "total_latency_ms": 200},
        ],
    )
    signals = store.scoring_signals()
    assert signals["a/model"]["success_rate"] == 0
    assert signals["b/model"]["success_rate"] == 1
