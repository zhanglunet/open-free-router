# Attribution and project notice

`open-free-router` in this repository is derived from the original
[`NoelJudeNoel/open-free-router`](https://github.com/NoelJudeNoel/open-free-router)
project.

The original project and this derivative are distributed under the MIT License;
see [`LICENSE`](LICENSE). The current repository adds and maintains, among other
changes, a secure Codex Responses API bridge, third-party model catalog sync,
local proxy authentication, Gemini tool-call compatibility, provider refresh
hardening, expanded tests, and the project documentation website.

This repository is independently maintained by
[`zhanglunet`](https://github.com/zhanglunet). It is not an official OpenAI,
Cloudflare, or upstream model-provider product.

## Attribution: honest labeling design

The three-state honest-labeling idea behind `HONESTY_LABELS` — marking a
conclusion as extracted (measured this run), inferred (derived by an open
rule), or ambiguous (missing/conflicting evidence) — is borrowed from
[Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify),
licensed under the Apache License 2.0 (see its
[LICENSE](https://github.com/Graphify-Labs/graphify/blob/main/LICENSE)). No
source code was copied: the label vocabulary, the assignment matrix and all
implementations in this repository are original (see
`docs/plans/2026-10-25-doctor-honest-labeling.md`). The Apache License 2.0
text is available at the link above and is preserved by reference here.
