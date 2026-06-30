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

## 后处理

- **标准关联 (Standard Linking)** — 审核后处理：对每个引用了标准的 `AuditIssue`，在知识库中定位该标准文档，回填 `StandardRef`（doc_id / page_number / chunk_text）。best-effort——任何步骤失败都不影响审核结果。入口 `services/standard_linker.py: link_standards(issues, kb_ids, *, extractor=None)`；默认 extractor 为轻量 DeepSeek 模型（`extract_standards_deepseek`），可注入以便测试关联策略而无需 LLM。

## 知识库检索

- **文档向量化 (Document Embedding)** — 单篇 KB 文档被分块、生成向量并缓存的生命周期。它的完成是文档可被纳入检索的**前提**，但**不等于**检索已可用。终态称"**已向量化 (embedded)**"。
  _Avoid_: "就绪""ready""indexed"——历史上同时被用于文档层与知识库层，造成重载歧义。
- **知识库检索索引 (KB Search Index)** — 一个知识库内全部文档向量合并而成的检索服务可用性。它就绪表示该库此刻可被向量检索。终态称"**可检索 (searchable)**"。
  _Avoid_: "就绪""ready"——必须与文档向量化层的终态严格区分。
- **两者关系** — 文档向量化是知识库检索索引的**构成材料**（前置条件），不是同一回事：全部文档已向量化 ≠ 该库可检索，仍需合并建索引。类比："砖都烧好了 ≠ 墙砌好了"。
