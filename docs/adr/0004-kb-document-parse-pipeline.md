# KB 文档解析流水线：单一解析入口 + chunking 与页码解耦

历史上 KB 文档导入流程存在**双解析器不一致**——`extract_text`（用于向量索引）走 PaddleOCR / MinerU / pdfplumber 多级降级，`extract_text_by_page`（用于按页文本）只走 pdfplumber，且两者各自被调用方独立使用、从不交叉。同份 PDF 在一次导入里被解析两次，结果不共享。扫描版 PDF 上 `extract_text_by_page` 返回空列表（pdfplumber 抽不出扫描件文字），导致 `metadata.page_texts = [""]`——按页文本结构在 KB 文档层是缺失的。下游的标准关联回填路径需要这个字段才能把 `page_number` 写进 issue，于是 `page_number` 永远是 `None`，前端 PDF 跳转链接 `?page=` 为空，审核员点链接后停在首页看不到引用页。

本次重构在 Issue #29 给出系统级重新设计；本 ADR 钉死三个关键取舍，每个都对应"未来读者回看时疑惑"的决定。

## Considered Options

### 取舍 1：存量 KB 文档不自动迁移

- **保留用户手动触发（采纳）** — 上线后 157 个老 KB 文档的 PDF 跳转 page= 仍然为空，直到 KB 管理员在前端逐个点击"重新解析"按钮修复。OCR 配额由用户决定是否消耗。
- **上线时一次性全量重建** — 后端启动时或一次性脚本对所有 KB 文档跑 PaddleOCR + 重建索引。一键到位但消耗配额、有 GPU/CPU 抢资源风险、迁移失败需要重试机制。
- **强制每次审核前 reparse** — 用户打开审核报告前自动 reparse 所有相关 KB 文档。每次审核都消耗配额，体验差。

### 取舍 2：chunking 与页码解耦（按语义切 + 事后注入 page_number）

- **整篇切 + 事后 grep 注入 page_number（采纳）** — `Document(text=full_text)` → MarkdownNodeParser 或 SentenceSplitter 切 chunk → 每个 chunk 文本前缀在 `by_page` 里 `str.find()` 定位页号。跨页章节不被腰斩。
- **按页硬切（现状）** — 每页独立成 Document 后切 chunk。`page_number` 天然有，但跨页章节（如 GB 50034 的"5.2 照明标准值"横跨 2 页）被腰斩成多个 chunk，每个只含部分语义，向量召回"半个章节"而不是"完整章节"。
- **按页切 + 事后按 heading 拼接** — 需要"识别同一章节"算法（heading 路径匹配 + 邻接判断 + 特殊字符处理），复杂度高，D4 路径已能解决问题。

### 取舍 3：rga 文本搜索路径退役而非修复

- **删 `_run_rga`、用 `pages/{doc_id}.json` 内存 grep（采纳）** — `search_doc_by_text` 改用 pages 文件做 `str.find()`。删除 ~60 行 `_run_rga` 死代码。
- **修 rga 路径让生产环境能装上** — rga 是 ripgrep-all 二进制扩展，需要单独安装包、提供 PDF/DOCX 等多格式解析支持。即便装上也无法解决 `page_number=None` 问题（rga 输出"行号"在 PDF 上不映射到物理页）。
- **改用 BM25 + 倒排索引** — 对"标准编号精确匹配"这种需求是过度工程。纯 Python 字符串 find 在 KB 规模（<10K 文档）下毫秒级。

## Consequences

### 取舍 1 后果

- **正向**：避免上线时一次性 OCR 配额消耗（157 文档 × PaddleOCR 调用）；无大规模迁移失败处理负担；用户掌握"何时消耗配额"的控制权。
- **代价**：本次重构上线后老 KB 文档的 bug **不会自动修复**。审核员在所有存量 KB 文档被管理员点过重解析之前，仍会遇到 `?page=` 为空的链接。
- **必须配套**：前端必须提供"重新解析"按钮（Issue #29 API-a），否则管理员无修复入口。前端二次确认弹窗必须明示"消耗 OCR 配额"。
- **必须文档化**：本次重构的"为什么 page= 仍为空"问题答——"用户未触发修复"——不能被未来读者误以为"上线后自动好了"。本 ADR 即此文档化载体。
- **状态机副作用**：P2 实施后，未重新解析的文档 `embedding_status=embedded` 但 `node.metadata["page_number"]=None`。前端跳转对这些文档仍停在首页，直到重新解析。这是合法的中间态——`embedded` 表示"已向量化"而非"页码完整"。

### 取舍 2 后果

- **正向**：跨页章节（如 GB 50034 第 5.2 节跨 2 页）作为单个 chunk 完整存在；向量召回的 chunk 包含完整章节语义；MarkdownNodeParser / SentenceSplitter 的现有切块逻辑保持不变（不感知页边界）。
- **代价**：chunking 决策与页码标注是两个独立的步骤，`index_document` 内部增加一次 `O(N×M)` 的字符串 find（N=页数、M=chunk 数）。157 文档 × 100 页 × 100 chunk ≈ 1.5M 次字符串 find，毫秒级可接受。
- **精度退化**：chunk 文本前缀 `[:200]` 在 `by_page[*].text` 里 `find()` 可能找不到（OCR 修复后文本变化、chunk 跨页导致前缀落在页边界）。找不到时 `page_number=None`，前端降级为跳首页。这是可接受的精度损失——比"按页切破坏语义检索质量"轻得多。
- **字段含义变窄**：`page_number` 从"chunk 在哪页"降级为"chunk 起始文本在哪页"。对前端 PDF 跳转足够（用户看到第一页就能滚动看完整章节），对精确高亮不够——精确高亮需要 layout bbox（已在 Issue #29 预留）。

### 取舍 3 后果

- **正向**：删除 60+ 行死代码（`_run_rga` 函数及其调用方）；`search_doc_by_text` 不再依赖任何外部子进程；page_number 天然从 pages 文件取出而非从 rga 行号伪造。
- **代价**：失去 rga 的多格式支持能力（PDF/DOCX 内部解析）。但项目实际只需搜索 KB 文档（PDF/DOCX），不需要搜索任意二进制。KB 文档全文已经解析到 pages 文件，rga 的多格式能力在此场景下无价值。
- **数据一致性**：`search_doc_by_text` 与 `index_document` 现在从同一份 pages 数据消费——一份数据两个用途，彻底消除文本搜索与向量索引的不一致问题。
- **性能边界**：纯 Python `str.find()` 在 KB 规模 <10K 文档下毫秒级。规模增长后考虑引入倒排索引；当前 YAGNI。

## 关联

- **Issue #29**：系统级重新设计 PRD，含所有 10 个设计决定、11 条 User Stories、4 个实施阶段（P1 → P2 → P3 → P4）。
- **AGENTS.md / `## 关键修复记录`**：本次重构沿用项目"GPU 显存管理"、"按需加载 reranker"等模式，OCR 缓存层也遵循"按需加载"原则。
- **CONTEXT.md**：P4 实施后需新增 4 个术语——KB 文档解析、ParseResult、按页文本、重新解析。