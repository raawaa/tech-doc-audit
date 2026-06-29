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
- **四个 agent 工具** — `search_kb`（语义搜索）/ `search_kb_text`（精确文本搜索）/ `read_chapter`（章节阅读）/ `flag_issue`（记录问题）。

## 后处理

- **标准关联 (Standard Linking)** — 审核后处理：对每个引用了标准的 `AuditIssue`，在知识库中定位该标准文档，回填 `StandardRef`（doc_id / page_number / chunk_text）。best-effort——任何步骤失败都不影响审核结果。入口 `services/standard_linker.py: link_standards(issues, kb_ids, *, extractor=None)`；默认 extractor 为轻量 DeepSeek 模型（`extract_standards_deepseek`），可注入以便测试关联策略而无需 LLM。
