# Model Availability Test Feedback (2026-08-05)

25 registered models tested. 20 available, 5 unavailable (removed from registry.yaml).

## Unavailable

| Model | Provider | Error |
|-------|----------|-------|
| gq/groq/compound | groq | 403 |
| gq/groq/compound-mini | groq | 403 |
| nv/minimaxai/minimax-m3 | nvidia-nim | timeout |
| nv/stepfun-ai/step-3.7-flash | nvidia-nim | 403 |
| nv/z-ai/glm-5.2 | nvidia-nim | 403 |

## Bug: Stale Quota_Exhausted Lockout

nova/deepseek-v4-flash returned 503. Root cause: sensenova:slot-0 locked as quota_exhausted despite healthy upstream (direct curl 200 OK). Lockout from transient 503s during refresh, no auto-recovery.

Workaround: open-free-router resilience reset --provider sensenova

Fix: auto-reset quota_exhausted after N successful upstream probes.

## Discover: 46 providers / 304 free models

Run open-free-router discover --test --adopt to adopt candidates.

## Change Applied

Excluded 5 unavailable models from registry.yaml. Backups: registry.yaml.bak-20260805-*
