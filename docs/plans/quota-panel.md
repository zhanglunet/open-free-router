# PRD-096 T3：已有额度观测面板

依赖基础修复 PR #66；需求 #58、T1 #56（精确数字仅本地，公开只展示档位）。

本轮读取本地网关已持久化的归一化响应头，不增加采集或请求。`/api/routing` 从受认证的代理 `/api/resilience` 读取后派生视图；不直读状态文件。requests 与 tokens 单位分别展示 limit / remaining / reset_at；上游未提供 used，因此始终 null，不用本机分析计数相减。没有观测的 provider 为 unknown，时间缺失、未来或超过 1800 秒为 stale，边界 1800 秒仍新鲜。tight 为比率 <0.10，exhausted 为比率 0，与 cost 降权边界一致；仅提示，不改变熔断状态。

历史数字可在本机查看，但 stale 标签与有效期清晰可见，不作为当前余额。`public_quota_bands` 是允许列表投影，只输出 provider 和档位；本轮不把任何运行时快照接入公开站点构建，也不发布静态额度数据。

验证：`pytest tests/test_quota_view.py tests/test_ui_auth.py tests/test_proxy_resilience_api.py -q`；`node --check src/open_free_router/web_static/static/js/app.js`。运行 `PYTHONPATH=src .venv/bin/python scripts/render-quota-fixture.py` 生成五档合成数据预览，仅用于本地视觉核查，不等于真实服务商验收（#60）。
