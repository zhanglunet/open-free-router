"""Generate an offline synthetic panel for visual QA; no upstream requests."""
import json
from pathlib import Path
from open_free_router.quota_view import quota_view
from open_free_router.resilience import ResilienceManager
from open_free_router.quota import parse_quota_headers

root = Path(__file__).resolve().parents[1]
manager = ResilienceManager()
for provider, remaining, observed in [('synthetic-sufficient', 80, 1000), ('synthetic-tight', 5, 1000), ('synthetic-exhausted', 0, 1000), ('synthetic-stale', 75, 1)]:
    manager.record_quota(provider, 0, parse_quota_headers({'x-ratelimit-limit-requests':'100', 'x-ratelimit-remaining-requests':str(remaining), 'x-ratelimit-reset-requests':'60s'}, 200, now=observed))
view = quota_view(manager.snapshot(), ['synthetic-unknown'], now=2000)
js = (root/'src/open_free_router/web_static/static/js/app.js').read_text()
render = js[js.index('function renderQuota('):js.index('function renderRouting(')]
helpers = js[js.index('function esc('):js.index('function getAuthToken(')]
css = (root/'src/open_free_router/web_static/static/css/style.css').read_text()
html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>'+css+'</style><main style="padding:24px"><h1>免费额度观测 · 合成数据核查</h1><p>仅测试响应头观测；非真实服务商验收。unknown 表示未上报。</p><article class="panel"><div id="quota-list" class="runtime-list"></div></article></main><script>const byId=(id)=>document.getElementById(id);'+helpers+render+'renderQuota('+json.dumps(view)+');</script></html>'
output = root / 'artifacts/quota-synthetic.html'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(html)
