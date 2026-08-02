import time

from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner


def test_route_planning_p95_stays_below_five_milliseconds():
    registry = Registry({
        f"provider-{provider}": {
            "prefix": f"p{provider}",
            "api_key": "benchmark-placeholder",
            "models": [
                {"id": f"model-{model}", "tool_calling": model % 2 == 0}
                for model in range(20)
            ],
        }
        for provider in range(5)
    })
    planner = RoutePlanner(registry, {"fallback": {"max_attempts": 10}})
    samples = []
    for _ in range(1000):
        started = time.perf_counter()
        planner.plan("auto/coding")
        samples.append(time.perf_counter() - started)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < 0.005
