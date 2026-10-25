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

## Attribution: BYOS (bring-your-own-subscription) approach

The BYOS access approach in this repository — attaching an existing
subscription through an OpenAI-compatible endpoint with an
environment-variable credential and an explicit opt-in adapter — is inspired
by [anomalyco/opencode](https://github.com/anomalyco/opencode), licensed under
the MIT License (see its
[LICENSE](https://github.com/anomalyco/opencode/blob/main/LICENSE)). No source
code was copied: the implementation in `src/open_free_router/byos/` is
original to this project. opencode's MIT notice is reproduced below as a
reference.

```
MIT License

Copyright (c) 2025 opencode

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
