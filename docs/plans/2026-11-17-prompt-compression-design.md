# PRD-477/T1：提示词压缩设计

> PRD：adopted/PRD-477-ofr-prompt-compression.md（issue #2305）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 压缩策略（T2）

确定性压缩（不用 LLM，保证可复算）：去空行/去重复空白/去 markdown 装饰字符/合并连续相同行。只做无损或近似无损的确定性压缩——不改词序、不改关键词。

压缩比可观测：`compression_ratio = len(original) / len(compressed)`。

## §2 保真门（T3）

压缩后保真度检查：关键词集合差集为空才算通过。

- 原文的关键词集合 = {w for w in re.findall(r'[\w]+', text) if len(w) > 2}（去停用词前的词干集）
- 压缩后的关键词集合必须 ⊆ 原文的关键词集合（不许发明新词）
- 不过门 → 自动回原文（硬门，不是可选项）

## §3 开关与降级（T4）

- 开关：prompt_compression_enabled 默认 **false**——可选开启；
- 失败即放行原文：压缩管线任何异常 → 放行原文（零阻塞）；
- 按模型/场景的启用范围：enabled_models 列表可配；不在列表中的模型不压缩。
