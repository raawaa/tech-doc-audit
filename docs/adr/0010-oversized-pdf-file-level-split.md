# 超限 PDF 在文件层分块解析、不去提工单把 SaaS 100 页上限提高

`#173`（PaddleOCR SaaS 100 页上限下的超限 PDF 分块解析）的 spec 在选型时把"为什么要走文件层分块解析"这一节的几个被拒方案拍死在 ADR 里——后续读者一旦忘了 `pdf_splitter.parse_split` 为什么会以这种形态存在，就会"修"出相反的设计。本 ADR 把被拒方案 + 选定方案钉死。

## Context

`#160` 调研发现 PaddleOCR SaaS 对 >100 页的 PDF **静默截断**：服务端只识别前 100 页、剩余静默丢弃，但 job `state=done`、`errorMsg` 为空，从响应里**完全无法与正常成功区分**。项目里躺着一批 `embedding_status=embedded`、看起来一切正常、实际只有前 100 页内容的 KB 文档——审核 agent 检索它们会拿到半截标准，还以为是全文。

同时，KB 管理员面前摆着几千篇 200+ 页的国标 / 行标——**恰恰是审核最需要的那批**。批量重新解析把它们放进 `skipped` 名单、reason 写 `page_limit`，等于把最有价值的一批文档永久排除在库外，没有任何修复入口。

需要的是：① 超限文档能真正被解析完整；② 库里已存在的"假成功"能被找出来并修好；③ 整个过程消耗多少 OCR 配额在用户点确认前就说清楚。

## Decision

**文件层分块解析**（`core/pdf_splitter.parse_split`）：源 PDF 物理页数 > `PADDLEOCR_PAGE_LIMIT`（默认 100）时，在**文件层**用 pymupdf `insert_pdf` 切成若干 ≤99 页子 PDF、逐块送 PaddleOCR、再按源 PDF 物理页号缝合成单一 `ParseResult`。触发点选在 `_paddleocr_parse()` 内部、`_parse_pdf()` 路由层之外——文字层 PDF 无论多少页都继续走 PyMuPDF 零配额路径，不被这条规则波及。

缓存写在**源 PDF 的 sha256** 槽位上（`save_cached` 内部对源路径算 hash），下次跑同一篇直接命中、零配额。子块解析在缓存条目 `source` 字段打 `paddleocr_split`（与整篇一次性 OCR 的 `paddleocr` 区分），便于 bulk 报告分桶统计 OCR 消耗（实测桶必须同时计入两个，详见 CONTEXT.md §"实测 OCR 消耗"）。

批量的跳过判据从"超 `PADDLEOCR_PAGE_LIMIT`"改为"**单篇预估 OCR 页数**超过拆分成本阈值 `BULK_REPARSE_SPLIT_COST_LIMIT_PAGES`（默认 20000）"——超阈值的文档进 `skipped`，reason 为 `split_cost_exceeded`，语义是"这一篇太贵了，你自己决定"，而不是"做不了"。`--force` 不能绕过该阈值；要绕必须显式 `--ignore-cost-limit`。

存量假成功的修复通过 `scripts/repair_truncated.py` 实现：扫描所有 KB（`--kb-id` 可收窄）→ `core/truncated_doc_detector.find_truncated_docs` 识别"页数对账"不一致的文档（仅查 `embedding_status == "embedded"` 且走 PaddleOCR 路径的，PyMuPDF 路径不截断不入选）→ 先标 `truncated`（崩溃可恢复：下一次批量重新解析按 `embedding_status != "embedded"` 选取规则自然拾起）→ 调 `bulk_reparse_service.reparse_one` 走完整重新解析流程（解析 → 按页文本 → 重建索引 → 状态）。

页数对账用 `len(by_page) < 源 PDF 物理页数` 作判据，三处共用：`pdf_splitter.parse_split` 的每块自检、`_parse_pdf` 缓存命中后的判废、`truncated_doc_detector.find_truncated_docs`。`pdf_page_count` 在损坏 / 加密 / 非 PDF 时返回 `None`、跳过对账——不为读不到的信息发明一个坏结论。

## Considered Options（被拒方案）

### 方案 A：向 PaddleOCR 服务商提工单把 100 页上限提高

> **被拒** —— 服务端硬约束，不在本项目可控范围。即便对方愿意调，调高也只是把硬截断点推到一个新数字，**静默截断的语义不变**：job 仍然 `state=done`、`errorMsg` 仍然为空、项目端仍然无法区分正常成功与截断成功——这是 #160 调研的结论，不是工程努力能消除的。要把"截断 = 失败"变成可被本项目解析层拦截的语义，需要服务端同时改 `state` 或在响应里加 `truncated: true` 标志，跨厂商协调成本与等待时间都不可控。

### 方案 B：用本地 PaddleOCR 替换 SaaS

> **被拒** —— 架构级决策，GPU 与运维成本是另一个量级。本项目当前不在自托管 OCR 推理栈上：依赖 SaaS 是为了把 GPU / 模型更新 / 服务可用性等运维成本外包给服务商。改本地化涉及 GPU 采购 / 推理服务化 / 模型版本管理 / 显存管理 / 跨进程并发等一系列新问题，且本地 PaddleOCR **也不一定能突破 100 页 / 文件的限制**（同样的 100 页上限可能源于模型 context window 而非服务端故意截断——#160 调研结论）。即便本地能跑，OCR 路径换栈与本 spec（仅解决"超限文档如何被解析"）不是同一层问题。

### 方案 C：只解析前 100 页并接受内容损失

> **被拒** —— 即现状的"静默截断"，正是本 spec 要消灭的。把现状显式化为"已知损失"等于给"假成功"开一张长期许可证：审核 agent 永远拿到半截标准、`page_number` 永远指向被截断后的偏移、`standard_reference` 永远只能回填到截断范围；用户**完全看不出**这件事在发生（页数列显示 247，按页文本只有 100 页，两个数字从不并排出现）。这是 #160 / #170 复盘后明确拒绝的语义。

### 方案 D：信任缓存、不做页数对账

> **被拒** —— 会把历史假成功永久固化。`core.parse_document._paddleocr_jsonl_to_parse_result` 历史上不校验 `len(by_page) == doc.page_count`，意味着 #87 加 `PAGE_LIMIT` 拦截之前那批"假成功"文档的缓存条目（`source=paddleocr`、`len(by_page)=100`、`doc.page_count=247`）已经写盘了。如果"缓存命中直接返回"且不做页数对账，那么**任何后续 reparse 路径——包括文件层分块解析——都会先把那份被截断的缓存原样读回来**，结果是 `repair_truncated.py` 跑完一遍什么也没修、`embedding_status` 从 `embedded` 不变、按页文本仍只有 100 页、`split_cost_exceeded_warning` 不触发、`actual_ocr_pages` 不增加——修复机制本身变成 no-op。
>
> `_parse_pdf` 缓存命中后必须插入一道 `len(cached.by_page) < pdf_page_count(file_path)` 的判废：完整条目命中、零配额（Q4 成立）；历史截断条目判废重解析（Q5 成立）。这是这一整个 spec 里唯一能让 `repair_truncated.py` 真正生效的机制——把它砍掉等于把"修复"两个字从项目里删掉。

## 为什么值得记 ADR

下一位读者会"修"出四个相反的设计：

- 看到 PaddleOCR SaaS 的 100 页限制，会去提工单把上限提到 500 页 → 服务端硬约束，跨厂商协调成本与等待时间都不可控；且静默截断的语义不变。
- 看到 GPU 资源充足，会"顺便"切到本地 PaddleOCR → 架构级决策，OCR 路径换栈与本 spec 不是同一层问题。
- 看到 100 页限制只是 SaaS 端的"小坑"，会接受"只解析前 100 页" → 等于开一张"假成功"的长期许可证，下游永远拿到半截标准。
- 看到缓存命中已经写了 source 字段，会"信任缓存"不做对账 → 把历史截断缓存固化在索引里，`repair_truncated.py` 退化成 no-op。

四条都是刻意的。

## 取舍

- **正向**：超限文档能完整解析（对调用方透明，入口仍是 `parse_document`）；存量假成功可被发现与修复；预检准确告知"将拆 X 篇 / 共 Y 块 / 总 Z 页 OCR"；拆分路径下同一篇文档第二次跑命中缓存、零配额；页数对账统一在三处共用同一判据。
- **代价**：分块解析引入临时 PDF 落盘（`.scratch/split_pdfs/{run_uuid}/{sha256[:12]}/chunk_*.pdf`），需 reaper 回收；分块解析不并发（#161 Q6，单篇内顺序、跨 doc 仍 4 并发）；`actual_ocr_pages` 必须同时计入 `paddleocr` 与 `paddleocr_split` 两个桶（不计入会让被拆分文档在实测 OCR 消耗里凭空少几千页——#178 翻转的 #90 错法）。
- **状态机副作用**：存量被识别的假成功文档先标 `truncated` 再走重解析——`truncated` 是新的终态（与 `failed` 平行），与既有的**待重解析文档**名单的 `embedding_status != "embedded"` 选取规则相容，崩溃可恢复（`truncated` 自动被下次批量重新解析拾起）。
- **必须配套**：`.env` 提供 `BULK_REPARSE_SPLIT_COST_LIMIT_PAGES` 调阈值；`scripts/repair_truncated.py --dry-run` 提供无副作用报告；前端 preflight 响应 `BulkReparsePreflightResponse` 把"将被跳过"的虚警换成"将拆分 X 篇 / 共 Y 块"。

## Spec Out of Scope（明确不写进 ADR 的相邻议题）

- **`doc.page_count` 与源 PDF 实际页数不符时的回写**：本 ADR 不讨论也不实现——`doc.page_count` 是导入时由 `services/doc_service.py` 用 pdfplumber 写入的元数据，源 PDF 物理页数才是 OCR 层的真实真相。两者语义一致（同一份 PDF 的物理页数），但导入时的数字与解析时的数字可能因不同读取路径而漂移；本 spec 一律以 `core.parse_document.pdf_page_count(file_path)` 为准、`doc.page_count` 仅作 fast-path 缓存，**不**回写到 `doc.page_count`。
- **新的"特殊状态视觉徽章"**：本 ADR 不引入任何新的 UI 状态显示——`truncated` 是 backend 状态机的中间态（修复完成后落到 `embedded`），不是给用户看的徽章。前端只在 preflight 响应里看到 `will_split_docs`、在 `force_cost_exceeded_warning` 里看到被成本阈值挡住的篇数；任何"截断文档徽章"在 spec §Out of Scope。

## 关联

- **父图 #173**（已 close）— 系统级 spec，所有 46 条 User Stories / 4 个实施阶段（T01 → T10）。
- **子 ticket**：
  - `#160`（已 close）— SaaS 契约调研，确认 100 页是服务端硬约束且**静默**截断。
  - `#161`（已 close）— 拆分机制 + 缓存键策略拍板（文件层 / sha256 槽位 / `paddleocr_split` 分桶）。
  - `#170`（已 close）— 存量假成功的检测与 `truncated` 状态机拍板。
  - `#162`（已 close）— bulk 目标选取 + force 模式 + 拆分集成拍板（`PAGE_LIMIT` → 拆分触发点；跳过判据改为拆分成本阈值）。
- **实施 ticket**：
  - `#174`（已 close）— T01 术语 + 配置底座：`PADDLEOCR_PAGE_LIMIT` 等 4 个 import-time 常量集中到 `core.settings`。
  - `#176`（已 close）— T03 `core/pdf_splitter.py` + `parse_split` wiring。
  - `#178`（已 close）— T05 `actual_ocr_pages` 把 `paddleocr_split` 计入（与本 ADR "取舍"段对齐）。
  - `#179`（已 close）— `core/truncated_doc_detector` 唯一判定入口。
  - `#180`（已 close）— `scripts/repair_truncated.py` 薄 wrapper。
  - `#181`（已 close）— `services/bulk_reparse_service` 成本预算 + 硬改名 `over_page_limit` → `split_cost_exceeded`。
  - `#182`（已 close）— API + CLI + frontend surface（preflight / trigger / types）。
  - **本 ticket #183**（进行中）— T10 术语 + ADR 本体落地。
- **CONTEXT.md** — 新增四个术语（PDF 分块解析 / 解析分块 / 页数对账 / 拆分成本阈值）+ 文档向量化条目补 `truncated` 状态；OCR 成本预检条目更新语义（PAGE_LIMIT = 拆分触发点，不再是跳过）。
