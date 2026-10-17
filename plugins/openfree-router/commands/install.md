---
description: 一键安装 openfree-router 本地代理并同步 9 个客户端
---

# 安装 openfree-router

在插件目录（`plugins/openfree-router/`）下按顺序执行以下步骤。
某一步失败就停下，把报错原文告诉用户，不要继续往下走。

1. `bash scripts/install.sh --auto-discovery` —— 克隆仓库、创建 venv、pip 安装
   （脚本与仓库根 `scripts/install.sh` 同步，保证插件目录自包含）
2. `open-free-router setup` —— 交互式填写各家 API key（可选；无 key 也可先跑起来）
3. `open-free-router serve` —— 启动 proxy(8337) + UI(9057) + 定时探测
4. `open-free-router sync` —— 同步本机已安装客户端的配置
   （9 客户端：Codex / Claude Code / OpenCode / Hermes / Kimi CLI / OpenClaw / WorkBuddy / Pi / OMP，
   同步命令与验证命令见 `clients.yaml`）
5. 验证：`curl -s http://127.0.0.1:8337/health | grep -q ok && echo proxy-ok`

全部通过后，告诉用户代理地址（http://127.0.0.1:8337）和面板地址（http://127.0.0.1:9057）。
