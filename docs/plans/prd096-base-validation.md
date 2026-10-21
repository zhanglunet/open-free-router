# PRD-096 基础分支独立验证修正

#66 在观测新鲜度修复之外，仅前向同步已在 #68 验证的五项旧基线测试修正，以及固定原 generated_at 的静态 catalog 产物。未下沉面板、策略实现或 #68 整体提交；PR base refs 保持 #66→main、#67→#66 分支、#68→#67 分支，不改 main，不 force-push，不合并 GitHub PR。

- 三份测试文件：`test_error_unify.py` 通过实际 executor 验证结构化 TimeoutPhase，不恢复按文字猜测的旧 helper；`test_executor.py` 精确断言默认 429 同候选重试后的 3/2 次尝试与同槽请求次数；`test_proxy_hardening.py` 显式关闭重试以独立验证原 Retry-After 透传断言。断言未放宽。
- `site/data/catalog.json`：只补既有生成器从 `docs/provider-profiles.json` 导出的静态 profile.free_tier。固定原 generated_at，二次导出字节一致；没有凭据和每槽运行时额度。
- 本地执行相同 catalog 生成检查、CLI `--version`、`models --json` 成功。Python 相关单元/集成回归在各自精确 head 验证；远端各层检查必须独立到终态，不能借用顶层成功。

全部合成数据；不执行 #60 真实服务商调用，不发布或部署。
