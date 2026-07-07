# 不为旧 KB 全量一次性回填 block_range

切分与版面布局是两条独立的流水线（PRD #29 / ADR-0004）：chunk 由 `MarkdownNodeParser / SentenceSplitter` 切，layout blocks 由 PaddleOCR 切。要让 chunk 高亮走坐标路径，必须在 KB 索引时把 chunk 文本"对齐到"layout blocks，存进 `node.metadata.block_range`。但是存量 KB 索引里**没有**这个字段——若一次性迁移所有旧 KB，需要为每个 KB 调 `parse_document` + 重算 `block_range` + `rebuild_kb_index`。

我们**故意不**做这件事：
1. **OCR 配额是不可再生成本**。每个文档的全量重算要跑一次 PaddleOCR（哪怕命中缓存，IO / CPU / 锁依然可观）。批量迁移等同于强制全库 reparse。
2. **迁移只换高亮路径的覆盖率**，不影响功能正确性——旧 chunk 在没有 `block_range` 时运行时 fallback 到原 `matchHighlightToBlocks` 字符串匹配路线（详见 `frontend/src/lib/layoutMatch.ts`）。`lcsRatio >= 0.85` + `MIN_LCS_LEN = 4` 的兜底虽然不完美，但历史上没有"高亮错位"的真实事故报告。
3. **新 chunk / 重 index 的 KB 自动有 `block_range`**——只要 `index_document` 同步计算并写入 metadata，**新流程**就是正向的。旧 KB 会随用户主动 `POST /kb-documents/{doc_id}/reparse` 逐步补齐。

迁移一旦开跑就难以撤回（OCR 配额已经被消耗），符合"难反转"。没写过这份 ADR 的话，下一位工程师看到 `_inject_block_range()` 里"只处理新 chunk"会以为是个 bug 然后写迁移脚本——OCR 配额就在没人注意的情况下被吃光了。

**取舍**：在过渡期内，同一份 KB 文档的不同 chunk 可能走两条高亮路径（新 chunk 走坐标、旧 chunk 走字符串匹配），用户能在某些 issue 上看到高亮精度差异。这是已知代价，不是 bug。

**未来撤销条件**：当 OCR 配额不再是成本约束（比如模型本地化、按需付费、或用户主动批准全量迁移）时，可以补一次性脚本回填所有 `block_range IS NULL` 的 chunk。