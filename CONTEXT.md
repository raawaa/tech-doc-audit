# CONTEXT.md

本项目的 ubiquitous language / 领域术语表。单上下文仓库（single-context）。
命名领域概念时使用此处的术语，勿漂移到同义词。术语在 `/grilling`、`/improve-codebase-architecture` 流程中被解析时惰性补充。

## 核心领域名词

- **知识库 (Knowledge Base, KB)** — 技术/标准规范文档库，向量检索与文本搜索的来源。一个审核任务可关联多个 KB。
- **待审核文档 (Audit Document)** — 用户上传、等待审核的招标/技术文档。与 KB 文档是两个相互独立的域。
- **审核任务 (Audit Task)** — 将一个待审核文档对照若干知识库执行的审核单元；后台线程异步执行。
- **审核结果 (AuditResult)** — 审核产出，含 `issues` / `summary` / `standard_reference` 等。
- **审核问题 (AuditIssue)** — 审核中发现的一个问题，挂在 `AuditResult.issues` 上。
- **标准引用 (StandardRef / `standard_reference`)** — `AuditIssue` 上挂载的、指向某项标准的引用（doc_id / page_number / chunk_text / standard_name / standard_id）。

## 审核执行

- **Agentic ReAct 审核** — LLM 在 ReAct 循环中自主调用工具完成审核。入口 `services/agentic_audit.py: run_agentic_audit()`。两条实现路径（native function calling / structured_llm）由 `LLM_PROVIDER` 选择。
- **四个 agent 工具** — `search_kb`（语义搜索）/ `search_kb_text`（精确文本搜索）/ `read_chapter`（章节阅读）/ `flag_issue`（记录问题）。前两个是 KB 查找工具，审核与问答共用（实现将集中在 `services/agent_tools.py`）；后两个是审核文档域、仅审核用，留在 `agentic_audit.py`。
- **对话跟踪 (Trace)** — 一次 agent 运行的完整对话记录（系统提示、每轮 tool_calls 及其结果、reasoning），运行结束 best-effort 持久化到 `data/audits/{doc_id}/tasks/traces/`（审核）或 `data/qa_traces/`（问答），用于事后诊断 agent 行为；写入失败不影响运行结果。
- **LLM prompt 字符阈值**（Wayfinder #114 / #119，由 `core.settings` 提供，`.env` 可覆盖；**单 provider 标量，无 provider 维度**）：
  - **`PROMPT_FULL_THRESHOLD`**（`LLM_PROMPT_FULL_THRESHOLD`，默认 30000）— 初始 user prompt 的全文/预览分支决策点：`len(parsed_content) ≤` 该值时嵌入文档全文，`>` 时改用预览+`read_chapter` 翻页。structured_llm 与 native function calling 两条路径共用 `_build_user_content()` 拼接模板。
  - **`PROMPT_PREVIEW_CHARS`**（`LLM_PROMPT_PREVIEW_CHARS`，默认 8000）— 走预览分支时，初始 user prompt 嵌入的字符数（`parsed_content[:PROMPT_PREVIEW_CHARS]`）。仅当 `>` `PROMPT_FULL_THRESHOLD` 时生效。
  - **`CHAPTER_MAX_CHARS`**（`CHAPTER_MAX_CHARS`，默认 8000）— `read_chapter` 工具**单次返回**的章节文本上限（`_format_chapter_text`），即翻页的页大小。与 `PROMPT_PREVIEW_CHARS` 默认同值但语义独立——前者约束工具输出，后者约束初始 user prompt 嵌入量。该值同时被插值进 LLM 可见的工具描述（`_build_tools_spec()` 每轮重建，`.env` 覆盖直接流进描述，不会再像 #123 那样漂移成写死的"4000字符"）。
  - **章节翻页 (Chapter Pagination)**（#123）— `read_chapter(chapter_index, offset=0)` 返回 `章节原文[offset : offset + CHAPTER_MAX_CHARS]`。被截断时末尾提示给出下一段的 `offset`；`offset ≥ 章节长度` 返回"已读完"哨兵。native function calling 的 `offset` 参数与 structured_llm 的 `AgentAction.chapter_offset` 同义，缺省均为 0（旧 session 不填也能工作）。工具结果里写明"本次请求 offset / 实际返回第 X-Y 字符"，落进 trace 后可确认翻页是否真的发生。
    _Avoid_: 把 `CHAPTER_MAX_CHARS` 读成"每章能被审核到的字数上限"——加 offset 之前它确实是那个语义（后半章不可达），现在它只是页大小。
  - **值之间的依赖关系**：把 `PROMPT_FULL_THRESHOLD` 调到 ≤ `len(parsed_content)`（如设为 0）会关掉全文分支；把 `PROMPT_PREVIEW_CHARS` 调大/调小不影响分支决策点；`CHAPTER_MAX_CHARS` 与前两者无函数依赖，但调小会让 read_chapter 工具返回更短、需要更多轮翻页。
  - **现状值 30000/8000/8000** 对 deepseek 128K context 窗口极度保守——如需放宽，改 `.env` 即可观察试验，不必发版。

## 后处理

- **标准关联 (Standard Linking)** — 审核后处理：对每个引用了标准的 `AuditIssue`，在知识库中定位该标准文档，回填 `StandardRef`（doc_id / page_number / chunk_text）。best-effort——任何步骤失败都不影响审核结果。入口 `services/standard_linker.py: link_standards(issues, kb_ids, *, extractor=None)`；默认 extractor 为轻量 DeepSeek 模型（`extract_standards_deepseek`），可注入以便测试关联策略而无需 LLM。

## 知识库检索

- **文档向量化 (Document Embedding)** — 单篇 KB 文档被分块、生成向量并缓存的生命周期。它的完成是文档可被纳入检索的**前提**，但**不等于**检索已可用。终态称"**已向量化 (embedded)**"。
  _Avoid_: "就绪""ready""indexed"——历史上同时被用于文档层与知识库层，造成重载歧义。
- **知识库检索索引 (KB Search Index)** — 一个知识库内全部文档向量合并而成的检索服务可用性。它就绪表示该库此刻可被向量检索。终态称"**可检索 (searchable)**"。
  _Avoid_: "就绪""ready"——必须与文档向量化层的终态严格区分。
- **两者关系** — 文档向量化是知识库检索索引的**构成材料**（前置条件），不是同一回事：全部文档已向量化 ≠ 该库可检索，仍需合并建索引。类比："砖都烧好了 ≠ 墙砌好了"。

## KB 索引代码分层

KB 文档向量化与 KB 检索索引的概念边界（见上文 §知识库检索）落到三个模块：

- **`KBIndexWriter` (`core/kb_index_writer.py`)** — 写入 orchestrator。隐藏 chunking / metadata enrichment / page-number 注入 / block-range 注入 / embed 重试。公开 `index_documents(docs)`（单入口，批量形态）与 `rebuild_kb_index(kb_id, *, progress_callback=None)`（2-phase 编排：cached vectors fast-path → GPU re-embed fallback）。单篇 reparse 也走 `index_documents([doc])`，不再有单篇/批量两条路径。
- **`KBIndexStore` (`core/kb_index_store.py`)** — 一个 KB 的 FAISS + sidecar meta + vector cache + per-KB 锁的全部承载者。`KBIndexStore.open(kb_id)` 显式单例化（跨调用方共享锁与 `VectorStoreIndex` 缓存）。外部不再 import 私有锁。
- **`ChunkLayoutMapper` (`core/chunk_layout_mapper.py`)** — 单方法 `map_chunk_to_blocks(chunk_text, page_layout) -> tuple[int, int] | None`，封装 T1/P2 规则。`_inject_block_range`（"对所有 node 注入"循环）留在 `KBIndexWriter`。

支撑模块：
- `core/text_norm.py` — `norm()`、`lcs_len()`、`_block_matches_chunk` + 阈值常量 `_MIN_LCS_LEN=4` / `_LCS_RATIO_THRESHOLD=0.85`。T1/P2 规则的唯一算法来源；与前端 `frontend/src/lib/layoutMatch.ts` 共享同一 JSON fixtures。
- `core/embed_retry.py::embed_batch_with_retry(client, batch)` — `tenacity 3×2s→30s`，仅重试 `APIConnectionError` / `APITimeoutError`。ADR-0007 瞬态失败语义的唯一 owner；`KBIndexWriter` 当前唯一调用方，但函数本身可被 `core/siliconflow_client` 复用。
- `storage/doc_repo.py::mark_doc_embedding_failed(kb_id, doc_id, err=None)` — doc `embedding_status="failed"` 状态转移的唯一公开入口（取代历史上 `core.index_manager._mark_doc_embedding_failed`）。
- `core/text_norm_fixtures.json` — T1/P2 + norm + lcs_len 的 Python/TS 共享测试用例；任一端实现漂移即测试红。

`core/index_manager.py` 删除（项目沿用硬改名惯例，issue #156/155/157/158 均为单 PR 一次性 sed）。

_Avoid_: 直接 `KBIndexStore(kb_id)`（绕开 `open()`）——单例化失效，锁不再跨调用方共享；继续 import `core.index_manager.index_document` / `index_documents_batch` ——Q1 决策后这两符号已不存在。

## 线上 Embedding/Rerank 降级语义（ADR-0007）

- **降级语义 (Degradation Semantics)** — 线上 embedding/rerank 服务失败时的整体行为契约：embedding 失败即抛（**无自动兜底**），rerank 失败优雅降级回原始排序。运维级兜底 = 重启切换 `EMBED_PROVIDER`。
  _Avoid_: 把"降级"理解为自动切换 provider —— ADR-0007 明确拒绝自动回退 local bge-m3 / 双供应商（跨供应商向量漂移会污染共享索引）。
- **瞬态失败 (Transient Failure)** — 线上调用失败的两个子类：**HTTP 层错误**（429/408/409/5xx，由 OpenAI SDK 内置重试负责，尊重 `retry-after`）与**连接层错误**（`APIConnectionError`/`APITimeoutError`，由调用方 tenacity 层负责，仅批量路径）。每一类只有一个重试 owner，不得另起一层；查询路径零附加重试。
  _Avoid_: 把"瞬态失败"当"任何异常都重试"——不可重试错误（模型缺失、ValueError）禁止进入重试。
- **每稿隔离 (Per-Doc Isolation)** — 批量建库中单稿 embedding 失败不中止整批：该稿记 `failed`、原因进批量报表 failed 明细，其余稿继续。**reparse 即追跑通道**（OCR 缓存命中零配额），不设死信暂存。

## Chunk → Layout 映射与高亮坐标（V8 PRD #49）

- **block_range** — KB chunk 在 `node.metadata` 上的字段，类型 `Optional[tuple[int, int]] = None`，记 `(start_block_order, end_block_order)` 闭区间（同一 page 内），表示该 chunk 文本覆盖到的 KB layout blocks（参见 `core.parse_document.Block.block_order`）。`start_block_order` 取 chunk 文本首次出现在 page 内的那一个 layout block；`end_block_order` 取最后一次。索引阶段由 `core.index_manager._inject_block_range(nodes, by_page)` 写入，与 `page_number` 同级（同一处调用点）。`None` 表示：注入失败 / KB 非 PDF（如 `.md`）/ 旧 KB 未触发 reparse——这种情况下高亮走原有 `matchHighlightToShapes` 字符串匹配 fallback。V8-S1 实现为 no-op，所有 chunk 的 `block_range` 暂时一律 `None`。
- **standard_block_range** — `IssueResponse.standard_block_range: Optional[tuple[int, int]] = None`，从 `issue.standard_reference.block_range` 拷贝（旧 issue / `standard_reference=None` 时为 `None`）。供前端 `PdfViewer` 直接读坐标画高亮、跳过字符串匹配。
- **正向高亮 (Forward Highlight)** — 高亮坐标**追溯自 agent 召回的 chunk**，即 KB 索引阶段已记录的 `block_range`，经 `flag_issue` / `standard_linker` 路径自动落到 `StandardRef.block_range`。前端 `PdfViewer` 拿坐标直接画，不经字符串匹配。
  _Avoid_: 把"高亮"等同为"字符串反查"——`matchHighlightToBlocks` 仅在 `block_range` 缺失时作 fallback；命名上不要再混用"高亮=反查"。
- **Block 范围回填 (Block-Range Backfill)** — 仅在 `index_document` / `rebuild_kb_index` / `reparse` 触发**重新嵌入**时同步写入 `block_range`。**不**为旧 KB 写一次性全量迁移（详见 `docs/adr/0005-no-one-shot-kb-block-range-backfill.md`）：存量 chunk 暂留空 `block_range`，运行时 fallback 到字符串匹配，逐步随 reparse 补齐。
- **MVP 边界** — 允许 `start == end`（单 block）、`start < end`（多 block 合并高亮）；跨页 chunk 仅记录起始页的 `block_range`（后续 reparse 链路再扩展）。

## KB 文档解析流水线（PRD #29）

- **KB 文档解析 (Parse)** — 单份 KB 文档进入流水线被结构化解析的全过程，入口 `core.parse_document.parse_document(path) -> ParseResult`。一次解析产出 `{by_page, full_text, layout}` 三类结果，被向量索引、按页文本存储、文本搜索等下游共用——避免历史上双解析器（`extract_text` + `extract_text_by_page`）导致的不一致。
  _Avoid_: 在新代码里再开一个"按页路径 vs 全文路径"分支——那等于回到 V1 之前的不一致状态。
- **ParseResult** — 文档解析的结构化结果（dataclass 集合），含 `by_page: list[PageText]`、`full_text: str`、`layout: list[PageLayout]`。JSON 序列化由 `to_dict()` / `from_dict()` 处理。所有结构化中间产物（缓存 / pages_store / reparse_service）共用此格式。
  _Avoid_: 在新代码里用 `dict` 自己构造"页面列表"——绕开 dataclass 会让 bbox 归一化 / layout polygon 等不变式难以维护。
- **按页文本 (Pages / `pages/{doc_id}.json`)** — KB 文档**按物理页组织的文本与版面**数据，持久化在 ``data/kbs/{kb_id}/pages/{doc_id}.json``。覆盖三类消费方：① `kb_files.py:/{doc_id}/page/{N}` 按页文本 API；② `vector_search.search_doc_by_text` 精确文本搜索（mem grep，无 rg/rga 外部依赖）；③ `reparse_service` 全量重建流程的输入。schema 含 `by_page`、`full_text`、`layout`，并附 `file_hash` / `model_version` / `parsed_at` 元字段。
  _Avoid_: 把按页文本写在 `doc.metadata["page_texts"]` ——metadata 字段会随布局/坐标增长而膨胀，且无 schema。把 layout / bbox 数据也存在 `metadata` 里——一并放在 `pages_store` 下保持关注点分离。
- **重新解析 (Reparse)** — 对单篇 KB 文档触发的一次性重建流程，入口 `POST /api/v1/kb-documents/{doc_id}/reparse`。流程：`parse_document` → `pages_store.save_pages` → 重建向量索引 → 更新 `embedding_status`。状态机：``pending_index`` → ``indexing`` → ``embedded``，失败回 ``failed``。故意**不**自动迁移存量 KB 文档——OCR 配额由用户决定是否消耗，详见 `docs/adr/0004-kb-document-parse-pipeline.md`（取舍 1）。
  _Avoid_: 在代码里写"导入时自动全量 reparse 现有文档"——这是 ADR-0004 明确拒绝的取舍，会无声消耗 OCR 配额。
- **批量重新解析 (Bulk Reparse)** — 对一个 KB 内全部**待重解析文档**触发的受控批量流程，是单篇**重新解析 (Reparse)** 之上的编排层：预检 → 确认 → 受控并发执行 → 终态统计。领域逻辑归属 `services/bulk_reparse_service.py`（目标选取 / 成本预检 / 页数上限分类 / 并发编排）；`scripts/bulk_reparse.py` 是它的**薄 CLI wrapper**，只负责 `.env` 加载、argparse 契约、终端渲染与退出码（0 全成功 / 1 有失败 / 2 dry-run 或取消）。与**重建索引 (rebuild_kb_index)** 的区别：后者只重建向量索引，不重新解析文档、不消耗 OCR 配额。
  _Avoid_: 叫它"批量索引" / "reindex all"——会与 `POST /{kb_id}/reindex` 混淆，两者消耗的资源完全不同（一个烧 OCR 配额，一个不烧）。
  _Avoid_: 在 CLI / API / 前端各写一份目标选取或成本估算——三条选取规则的第三条是 #93 复盘一整轮才补上的，第二份实现必然让它悄悄丢失。
- **待重解析文档 (Reparse Target)** — 满足三条选取规则**任一**的 KB 文档：① 未向量化（`embedding_status != "embedded"`）；② 缺按页文本文件；③ 按页文本存在但 `layout == []`（历史的 `_pdf_fallback()` 假成功残留；`force` 模式绕过三条规则，整库皆为目标）。`force` 模式绕过三条规则，整库皆为目标（换解析器后的整库重建入口）。
- **OCR 成本预检 (Preflight)** — 批量重新解析触发前的**无副作用**估算：命中 / 未命中缓存的篇数与页数、超单文件页数上限（`PAGE_LIMIT = 100`，issue #87）的清单。命中判定只有一份实现 —— `core.paddleocr_cache.cache_state_by_hash()`（按 `doc.content_hash` 查，不重算文件哈希），与 `get_cached` 同口径：条目损坏或 model_version 不符算未命中。历史 `source=fallback_pdfplumber` 条目按命中计（#99/05 后 V8 cache defense 已删除，运维清理是单独工单）。
  _Avoid_: 只探测缓存文件是否存在就报"命中"——会让清单与统计自相矛盾。
- **KB 检索状态写入者 (KB Index Status Writer)** — `core/kb_index_status.KbIndexStatusWriter` 是 `kb.index_status` / `kb.index_progress` / `kb.index_current_doc` 三个字段的**唯一写入者**（issue #147 / #148）。批量重新解析与单篇 reparse 不再各自手写读—改—写，也不再用 `caller_manages_kb_status` 这类布尔把这层责任在调用方之间切来切去 —— 编排层构造一个 `KbIndexStatusWriter(kb_id, total=...)` 后，整批期间只暴露 5 个 callback：`begin()` / `note_in_flight(name)` / `advance(done)` / `finish(failed=..., *, interrupted=...)` / `clear_building()`。`begin()` 写一次 `building` + `index_progress = 0`，期间只推进 `index_progress`（`done / total`，单调不减）与 `index_current_doc`（在飞文档名），末尾按结果写**唯一一次**终态（全成功 `searchable` / 有失败或中断 `failed` + 一行失败摘要，字符串格式收敛到 writer 内的 `_format_failure_summary` / `_format_interruption`）；崩溃自愈（`api.main.recover_stuck_indexes`）只调 `clear_building()` 把 KB 拉回 `none`，不动 doc 层的 `embedding_status`（那是 `doc_repo._save_doc_meta` 的事）。
  _Avoid_: 在 writer 之外的代码里直接 `kb.index_status = ...` / `kb.index_progress = ...` / `kb.index_current_doc = ...` / `kb_repo.update(kb)` 写这三字段 —— writer 的 API 不暴露"中途把 `index_status` 写成 `searchable`"的入口：批量跑到一半时 `searchable` 按定义不成立（"该库**此刻**可被向量检索"这句话只在终态一次性写），且前端 `KnowledgeBaseDetail.tsx` 以 `index_status === 'building'` 为轮询续订的唯一条件，途中闪 `searchable` 会让进度反复停轮询又重启（#93 实测 154 篇抖动上百次）。writer 的"整批只 `finish()` 一次终态"是这条不变式的执行而非提醒。
  - **`begin()` 归属（issue #155）** — 由 caller 独家承担：单篇路径在 `reparse_document` 构造 `total=1` writer 后**立即**调一次；批量路径在 `run_bulk_reparse` 批次开头调一次。`_reparse_async` / `_index_single_doc_async` 等 per-doc 线程入口**不再**调 `begin()` —— 否则 N 篇批量 = N+1 次 `begin()`，每次把 `_progress` 清零并把 `index_progress=0.0` 写盘，前端轮询会在两帧之间看到 `0/N` 短暂回退。同时 `begin()` 自身有 defense-in-depth：仅在"未开始过（`_progress is None`）"或"KB 当前 `status == 'none'`"时把 `_progress` 重置为 0.0；mid-batch 重复调用保留中间值，让 `_write` 的单调不减守卫自然兜住。
- **实测 OCR 消耗 (Actual OCR Usage)** — 批量跑完后**回收**的真实配额支出，与预检估算互为对照。地面真相是缓存条目的 `source` 字段（`core.paddleocr_cache.cache_source_by_hash()`）—— #93 事后正是靠数 `source=paddleocr` 的条目才证明"154 篇真跑了 OCR"，#110 把那次考古变成流程产物。四类分桶：`paddleocr`（真烧配额，唯一计入 `actual_ocr_pages` 的一桶）/ `cache_hit`（**跑之前**就已有条目，故缓存快照必须在触发任何解析前取）/ 其它解析器来源（`pymupdf` 等，零配额）/ `unknown`（跑完仍读不到条目）。页数取落盘 `pages/{doc_id}.json` 的真实 `by_page` 长度，读不到才回落预检估值。
  _Avoid_: 只看 `source` 就判"真实 OCR"——缓存命中的条目 `source` 同样写着 `paddleocr`，分不出来就等于把 0 页报成满页。
  _Avoid_: 读不到来源时默认按 OCR 记账——凭空补一个好看的页数正是 #90 报 1694 页而实际 0 页的错法，宁可显式记 `unknown`。
- **批量重新解析报告 (Bulk Reparse Report)** — 一次批量运行的结构化产出，落在 `data/kbs/{kb_id}/bulk_reparse_report.json`（`pages/` 的兄弟，`core/bulk_reparse_report_store.py` 管路径与读写）。内容：目标集与每篇的入选原因、预检估算与实测消耗**并列**、done / failed / skipped 三类明细（各带 doc id、原始文件名、原因串）、起止时间与耗时、并发度与是否 `force`。只留最近一次，历史归档不在 v1。
  _Avoid_: 预估与实测背离时自动拦截执行——#91 的教训是"差异需要被看见"，不是"差异需要被自动处置"。
  _Avoid_: 把报告塞进 `kb.metadata`——与"按页文本不写在 `doc.metadata`"同源的理由：无 schema、随字段增长膨胀。

## 跨层坐标语义

代码层锚点（语义与用法见上文 §"Chunk → Layout 映射与高亮坐标"）：

- `node.metadata.page_number`（int, 0-based） — chunk 起始所在物理页号。
- `node.metadata.block_range`（`(start_block_order, end_block_order)`） — chunk 在该页覆盖的 layout block 区间。
- `IssueResponse.standard_block_range`（`(start, end)`） — API 暴露给前端，拷贝自 `issue.standard_reference.block_range`。
- `PdfViewer` URL 参数 `highlight=chunk_text`（保留） — 当 `block_range` 不可用时，`PdfViewer` 走原 `matchHighlightToBlocks` 字符串模糊匹配路线。

## QA 引用体验（V9 PRD #67）

- **内联引用 (Inline Citation)** — QA 回答中"依据来源"不是位于消息末尾，而是作为 `source-document` UIMessage parts 与 `text` parts 按出现顺序交错在 message.parts 中，由前端按顺序渲染成 inline chip。每个 chip 携带 `sourceId`（格式 `src_<doc_id_short>_p<page>`），AI SDK 用它跨 message 去重相同 doc 的多次引用。同一 chat stream 中同一 doc_id 只产生一次 chip（后端按 doc_id 首次出现 emit `source-document`）。
- **进度指示器 (Progress Indicator)** — 流式渲染中，`tool-*` parts 的 `input-available` / `input-streaming` 状态展现为带 spinner 的轻量提示条（"🧠 搜索中…"），与未来的 source-document 同位置、同 DOM 锚。当 `tool-output-available` 紧跟着 `source-document` 到达后，提示条**就地升级为 chip**（同 React key 复用 DOM，不重排），不展示 input/output JSON。
  _Avoid_: 当前遗留的 `QA.tsx` 中 `parts.map(...) startsWith('tool-')` 渲染成 `<details>` 折叠块（展开显示 input/output）——QA 页面**不应渲染**该形态。audit 场景可参考该形态但仅作 review。
- **Preview-on-hover** — chip 的悬浮展示取自 `QASource.content_snippet`；点击行为保持现状：新标签页打开 `/pdf-viewer/<doc_id>?block_range=…`。

## PDF viewer architecture（V9 PRD #68）

- **生产 PDF viewer** — `frontend/src/pages/PdfViewer.tsx` 基于 `@embedpdf/react-pdf-viewer` 的 `<PDFViewer>` drop-in 组件，由 `frontend/src/App.tsx` 用 `React.lazy` 挂在 `/pdf-viewer/:docId`（懒加载，embedpdf 独立成 chunk）。这是 auditor 点击审核结果 chip 后打开的页面，URL 契约 `?page=` / `?block_range=` / `?highlight=`。演进史（headless → drop-in）见 `docs/adr/0006-pdf-viewer-embedpdf-dropin.md`。
  _Avoid_: 高亮走 annotation plugin，不再有 headless 时代的 `[data-testid="highlight-rect"]` 百分比 div；测试断言改用 registry 句柄拿 `getAnnotations()`。
- **annotation rect 坐标** — drop-in Highlight 组件:CSS **顶原点**,`scale` prop 把 rect 当 PDF pt 转 CSS px。**不要 Y-flip**(`bbox_norm.y1 × pageH` 直接当 origin.y,不是 `pageH - y2×pageH`)。**必须预除 effectiveDPR** —— embedpdf 的 `scale` 是 `renderScale = cssScale × effectiveDPR`,直接传 PDF-pt rect 会被多乘 DPR(X/Y 偏 2x)。`PdfViewer.tsx` 的 `getEffectiveDpr()` 从 `scroll.getMetrics()` 读 `pageVisibilityMetrics[].scaled.scale / (visibleWidth / pdfPageW)`,在 import 时除 rect 校正。
  _Avoid_: 别用 `window.devicePixelRatio` —— 实测物理 DPR=1.25 但 embedpdf effectiveDPR=2,不相等(可能含 browser zoom 或其他倍率)。必须从 embedpdf 自己的 metrics 读。
  _Avoid_: 别把 `matchBlockRangeToBlocks` 的输出当 annotation rect 起点 — #66 复盘踩过的坑(它是画布像素坐标,不是 PDF pt)。`matchBlockRangeToBlocks` 现为死函数(无 caller),保留 `lib/layoutMatch.ts` 是因为 `blockMatchesHighlight` / `matchHighlightToBlocks` 仍在用。
- **annotation 必须显式 `commit()`** — `importAnnotations()` 灌进去的 annotation 默认 `commitState: 'new'`,默认不渲染(`Highlight` 组件的 paint 路径只画 `dirty` / `synced` 状态)。snippet 的 `autoCommit: true` 只对 `CREATE_ANNOTATION` reducer 生效,import 路径不走那条。修法:`importAnnotations(...)` 之后立刻 `commit()`,把状态推到 `synced`。
- **annotation z-index 要手动提到 page 之上** — embedpdf Highlight 默认 `zIndex: 0`(或 `onClick ? 1 : undefined`),与 page canvas 同级,按 DOM 顺序 annotation 会被 page 盖住(用户看到"高亮在页面后面")。`PdfViewer.tsx` 头部 `<style>` 把 `[data-embedpdf-managed="true"] > div:last-child` 拉到 `zIndex: 3` 常驻可见。
- **drop-in `documentId` 配置路径** — `PDFViewerConfig` 顶层**没有** `documentId` 字段。要让 embedpdf 用 URL docId(而不是自动生成 `doc-<ts>-<rand>`)标识文档,必须走 `documentManager.initialDocuments[].documentId`。否则 `onLayoutReady` 的 `evt.documentId` 跟我们的 docId 永远不等,所有 import 路径被 early-return 跳过。
- **onLayoutReady 闭包竞态** — `handleReady` 订阅 `onLayoutReady` 时形成闭包,捕获当时的 `annotationsToImport`(layout API 还没回时是空数组)。修法:`annotationsRef` 镜像 `annotationsToImport`,`onLayoutReady` 回调里读 ref 而非闭包变量,配合一个 `useEffect` 兜底(viewerStatus === 'ready' 且 annotations 非空时再 import 一次)。
- **E2E annotation 断言钩子** — `PdfViewer.onReady` 在 `import.meta.env.DEV` 下把 `PluginRegistry` 挂在 `window.__pdfViewerRegistry: Promise<PluginRegistry>`。playwright 跑 `npm run dev` 时可用,生产 build (`import.meta.env.DEV === false`) 不挂 window,不泄露。`frontend/e2e/pdf-viewer.spec.ts` 用 `page.evaluate` 调 `annotation.getAnnotations()` 断言数 / rect / color / commitState。`getRectPositionForPage(rect)` 是验真实渲染位置的官方 API。
- **体积与 code-split** — `@embedpdf/react-pdf-viewer` 把所有 plugin 打 bundle。`PdfViewer` 经 `React.lazy` 拆成独立 chunk,主 `index-*.js` 不再含 embedpdf。复测渠道 `npm run bundle:report`(读 `frontend/dist/assets/*` 与 `frontend/scripts/bundle-size-baseline.json` 对比 delta)。当前 delta +278 KB gzip(>+200 KB 验收线),build 同时打包 `worker-engine` + `direct-engine`,但 `worker: false` 下 `worker-engine` 是死重 → 拆 **#69** code-split 子 issue。
- **`?highlight=` 判定来源** — `PdfViewer.scanAllPagesByText` 复用 `lib/layoutMatch.blockMatchesHighlight(block, highlight)` predicate(与 `matchHighlightToBlocks` 同 T1+P2 / N3+includes+LCS 语义,只返回 boolean),与生产字符串匹配同阈值,不引入语义分叉。该 predicate 在 `frontend/src/lib/layoutMatch.test.ts` 有 9 条覆盖。
