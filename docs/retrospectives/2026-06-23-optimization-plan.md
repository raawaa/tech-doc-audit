# 审核管线优化计划

**日期:** 2026-06-23
**基于:** 差距分析（10 维度）+ 行业共识调研（商业/学术/开源）+ FutureAGI 生产实践 + 完整代码审计

---

## 总览

当前系统成熟度评分：**4.5/10**。目标：12 个月内达到 **7.5+/10**。

核心发现：**问题不在技术栈（LlamaIndex + FAISS + bge-m3 是对的），在架构范式。** 当前是"主题级、单次 LLM 调用"的模式，行业共识是"需求级、提取与评估分离、逐条引用验证"。

---

## Phase 0：立即执行（1-2 周，降低幻觉 + 可追溯性）

此阶段无需改架构，在现有代码上加约束和验证。

### P0.1 — 所有审核 LLM 调用设 temperature=0

**文件:** `core/settings.py`, `services/agent_audit.py`, `services/topic_audit.py`
**工作量:** XS

当前 `get_llm()` 不设 temperature，使用模型默认值（约 0.7-1.0）。审核结果必须可复现。

改动：
- `settings.py` 的 `_create_llm()` 中：Ollama 走 `options={"temperature": 0}`，OpenAI 兼容走 `temperature=0`
- `agent_audit.py:74` `structured_llm.chat(messages)` 之前确认 LLM 已配置 temperature=0
- `topic_audit.py:151` 同样确认

### P0.2 — 强制引用（Citation）为必填字段

**文件:** `models/llm_schemas.py`, `models/audit_task.py`, `services/topic_audit.py`
**工作量:** S

当前 `TopicIssue.standard_reference` 是 `Optional[StandardRef] = None`，所有子字段也可选。LLM 可以不提供任何引用就输出结论。

改动：
```python
# models/llm_schemas.py - TopicIssue
standard_reference: StandardRef = Field(...)  # 从 Optional 改为必填
cited_excerpt: str = Field(description="引用知识库原文的逐字片段")
document_position: Optional[str] = Field(default=None, description="文档中的条款编号或位置")

# models/llm_schemas.py - StandardRef
standard_name: str = Field(description="标准名称，如 'GB/T 12345-2024'")
standard_id: str = Field(description="标准编号")
clause: str = Field(default="", description="条款编号，如 '5.2.1'")
requirement: str = Field(default="", description="该条款的具体要求原文")
```

同步更新 `AuditIssue` 模型和 `_issues_from_schema()` 映射逻辑。

### P0.3 — 增加审核结果类型：证据不足 / 超出范围

**文件:** `models/audit_task.py`, `models/llm_schemas.py`, `services/topic_audit.py`
**工作量:** XS

当前 `AuditType` 只有 `compliance | completeness | consistency`。LLM 在证据不足时被迫从三个中选一个。

改动：
```python
# models/audit_task.py
AuditType = Literal["compliance", "completeness", "consistency", 
                     "insufficient_evidence", "out_of_scope"]
```

同步更新 `TopicIssue.type` 的 Field description 和 `AUDIT_SYSTEM_PROMPT`，明确要求"当证据不足时使用 insufficient_evidence，当文档内容超出知识库覆盖范围时使用 out_of_scope"。

### P0.4 — 不确定性记录（Degradation Audit Log）

**文件:** `models/audit_task.py`, `services/audit_task_service.py`
**工作量:** S

当前降级路径全部静默（MinerU→pdfplumber、FAISS→rga text、Agent topics→8 fixed topics）。用户不知道审核质量被降级了。

改动：
```python
# models/audit_task.py - AuditTask 新增字段
degradation_log: list[dict] = Field(default_factory=list)
# 每项格式: {"stage": "parsing", "primary": "mineru", "fallback": "pdfplumber", 
#            "reason": "MinerU API unreachable", "timestamp": "..."}
```

在 `text_extraction.py`、`vector_search.py`、`agent_audit.py` 的每个降级点追加日志条目。前端展示质量指示器。

### P0.5 — 修复 H1 标题检测

**文件:** `core/index_manager.py`
**工作量:** XS

`_has_markdown_headings` 用 `^#{2,6}` 匹配标题，MinerU 输出的 H1（`# 标题`）会被跳过，导致降级到 SentenceSplitter。

改动：正则改为 `^#{1,6}`。

### P0.6 — KB 搜索 top_k 从 3 提高到 6

**文件:** `services/vector_search.py`
**工作量:** XS

`search_by_keywords()` 默认 `top_k=3`，reranker 从 6 个候选中选 3 个。行业标准是反馈 5-8 个相关段落给 LLM。

改动：`top_k` 默认值改为 6。

### P0.7 — rga 不可用时 WARNING 级别日志

**文件:** `services/vector_search.py`
**工作量:** XS

当前 `_run_rga` 的 `FileNotFoundError` 走 `logger.debug`，运营者无法知道全文搜索降级。

改动：改为 `logger.warning`，并在降级结果中附加说明。

---

## Phase 1：范式转变——从主题到需求（2-4 周）

此阶段引入新的审核范式，与现有管线并行运行（feature flag 控制），不影响现有流程。

### P1.1 — 知识库需求提取器（离线预处理）

**新建文件:** `services/requirement_extractor.py`
**新建模型:** `models/requirement.py`
**工作量:** M

将 KB 标准文档拆成原子化可检查需求。这是离线一次性工作，结果持久化存储。

```python
# models/requirement.py
class AtomicRequirement(BaseModel):
    requirement_id: str              # "req_gbt_xxx_5.2"
    source_kb_id: str                # 来源 KB
    source_doc_id: str               # 来源文档
    source_clause: str               # "5.2"
    standard_ref: str                # "GB/T XXXX-2024"
    requirement_text: str            # "质保期自验收合格之日起计算，不得少于12个月"
    check_type: Literal["threshold", "exists", "equals", "range", "semantic"]
    expected_value: Optional[str]    # "≥12个月" (for threshold type)
    category: str                    # "quality_warranty"
    keywords: list[str]              # 用于匹配的关键词
    vector_id: Optional[str]         # FAISS 向量 ID
```

`requirement_extractor.py`：
- `extract_requirements(kb_id: str) -> list[AtomicRequirement]`
  - 读取 KB 中所有文档的 parsed_content / 原始文件
  - 用 LLM（temperature=0）按条款拆成原子需求
  - 支持人工审核界面（标记为 pending_review 的需求）
- `load_requirements(kb_id: str) -> list[AtomicRequirement]`
  - 从持久化存储加载已提取的需求
- `index_requirements(kb_id: str)` 
  - 将需求向量化存入独立 FAISS 索引
- `search_requirements(query: str, kb_id: str, top_k: int = 10) -> list[AtomicRequirement]`

### P1.2 — 需求锚定审核管线（与现有管线并行）

**新建文件:** `services/requirement_audit.py`
**工作量:** M

新的审核流程，通过 feature flag `USE_REQUIREMENT_AUDIT=true` 启用。

```
输入：已解析的投标文档 + KB ID 列表

Step 1: 文档分块（利用 structure_service 的章节/条款边界）
  -> chunk_document_by_structure(parsed_content, structure)
  -> 每个 chunk 带章节路径元数据

Step 2: 加载所有相关 KB 的原子需求
  -> requirements = []
  -> for kb_id in kb_ids: requirements += load_requirements(kb_id)

Step 3: 需求-文档匹配（双向检索）
  -> for each requirement: search in document chunks (FAISS)
  -> for each document chunk: search in requirement index (FAISS)
  -> 合并去重，得到 matched_pairs: list[(req, chunk)]

Step 4: 分类 matched_pairs
  -> matched: 有对应关系的 pairs
  -> unmatched_requirements: KB 要求但文档未提到的需求 -> 自动生成 findings
  -> unmatched_chunks: 文档提了但没有标准可依的段落 -> 标记

Step 5: 并行合规判断
  -> for each matched_pair (ThreadPoolExecutor):
      -> 确定性检查（check_type != "semantic"）
         e.g. threshold: 正则提取数值 → 比较 → YES/NO
      -> LLM 语义检查（check_type == "semantic" 或确定性无法判断）
         Prompt: "标准要求X，文档声明Y，判断是否满足"
         Structured output: {verdict: YES|NO|PARTIAL, reason, confidence}

Step 6: 引用验证（对所有 NO/PARTIAL 的 finding）
  -> 检查 clause_id 是否存在于源文档
  -> 检查 cited_excerpt 是否逐字匹配源文档（含 OCR 容错）
  -> 验证失败 → 重试一次 → 标记 REFUSED_AMBIGUOUS

Step 7: 生成 AuditResult（复用现有模型）
```

核心函数：
```python
def run_requirement_audit(
    task_id: str,
    doc_id: str,
    kb_ids: list[str],
) -> AuditTask:
    """需求锚定审核主入口。"""
    ...

def match_requirements_to_document(
    requirements: list[AtomicRequirement],
    doc_chunks: list[DocChunk],
) -> list[RequirementDocPair]:
    """双向向量匹配。"""
    ...

def check_pair(
    pair: RequirementDocPair,
    llm: BaseLLM,
) -> CheckResult:
    """对单个需求-文档 pair 做合规判断（确定性优先，LLM 兜底）。"""
    ...

def validate_finding(
    finding: AuditIssue,
    source_texts: dict[str, str],
) -> ValidationResult:
    """确定性引用验证。"""
    ...
```

### P1.3 — 结构感知文档分块

**文件:** 新建 `services/chunking.py`，修改 `core/index_manager.py`
**工作量:** M

当前分块是固定 512 token 滑动窗口，不感知文档结构。

改动：
```python
# services/chunking.py
def chunk_by_structure(
    parsed_content: str,
    structure: DocumentStructure,
    max_chunk_size: int = 512,
    overlap: int = 50,
) -> list[DocChunk]:
    """
    按章节/条款边界分块。
    - 每个 Chapter 是主要边界
    - Chapter 内按 Clause 边界细分
    - 仅当 Clause 超长时才在句末切分（用中文分隔符）
    - 每个 chunk 注入 section_path 元数据
    """
    ...

class DocChunk(BaseModel):
    chunk_id: str              # "ch_第四章_4.2_0"
    text: str
    section_path: str          # "第四章 > 技术规格 > 4.2 电气要求"
    chapter_number: Optional[str]
    clause_number: Optional[str]
    page_range: Optional[tuple[int, int]]
    doc_id: str
```

同步修改 `index_manager._split_document()` 使用结构感知分块（当 `DocumentStructure` 可用时），否则降级到当前的 SentenceSplitter。

### P1.4 — 中文感知分隔符

**文件:** `core/index_manager.py` `_split_document()` 和 `services/chunking.py`
**工作量:** S

当前 `SentenceSplitter` 使用默认的英语分隔符。中文需要：

```python
CHINESE_SEPARATORS = [
    "\n\n", "\n", 
    "。", "！", "？", "；",   # 中文句末标点
    "，", "、",               # 中文句中停顿
    " ", ""
]
```

在 `SentenceSplitter(separators=CHINESE_SEPARATORS, ...)` 中使用，防止跨句切分。

### P1.5 — Section Path 元数据注入

**文件:** `core/index_manager.py`, `services/chunking.py`
**工作量:** S

当前 chunk 元数据只有 `doc_id` 和 `source`（文件名 stem）。需要注入从 `structure_service` 获取的章节路径。

改动：在 `_split_document` / `chunk_by_structure` 中，将 `section_path` 写入每个 chunk 的 `metadata`。

### P1.6 — 语义化文档段落检索（替代关键词定位）

**文件:** `services/topic_audit.py`, `services/vector_search.py`
**工作量:** M

当前 `locate_paragraphs` 用精确关键词匹配（`re.escape(kw)`），遗漏同义词和语义相关但不含关键词的段落。

改动：
- 将投标文档按 P1.3 分块 → 向量化 → 存入临时 FAISS 索引（扩展 `temp_index_service`）
- 用审计主题的 prompt（或 HyDE 生成的查询）做语义检索
- 降级路径保留当前的关键词定位

### P1.7 — 审核 topic 补 few-shot 示例

**文件:** `services/topic_audit.py`, 新建 `data/audit_examples/`
**工作量:** S

当前所有 audit prompt 是 zero-shot。对本地 Ollama 小模型影响较大。

改动：
- 创建 `data/audit_examples/topics.json`，每个预定义主题含 1-2 个示例
- 在 `_audit_prompt` 的 system message 中注入 2-shot 示例
- 示例需领域专家审核

### P1.8 — 主题审核增加重试 + 区分"无问题"和"失败"

**文件:** `services/topic_audit.py`, `services/audit_task_service.py`
**工作量:** S

当前 `_audit_single_topic` 返回空 issues 的情况可能是：(a) 确实没发现问题，(b) LLM 调用失败了，(c) JSON 解析失败了。三种情况无法区分。

改动：
```python
def audit_topic(...) -> tuple[list[AuditIssue], bool]:
    """
    Returns:
        (issues, success)
        success=True  → issues 是有效的审核结果（可能为空，表示无问题）
        success=False → 审核失败，issues 为降级结果
    """
```

在调用层（`_audit_single_topic`）增加重试：失败后等 2s 重试一次，再失败等 4s 重试第二次。全部失败记录到 degradation_log。

---

## Phase 2：质量守卫（2-4 周）

### P2.1 — 确定性引用验证

**文件:** 新建 `services/citation_validator.py`
**工作量:** S

在 `topic_audit._issues_from_schema()` 之后、返回 AuditIssue 列表之前，插入验证步骤。

```python
# services/citation_validator.py
def validate_issue_citations(
    issue: AuditIssue,
    kb_docs: dict[str, str],     # doc_id -> full text
    audit_doc_text: str,
) -> ValidationResult:
    """
    检查：
    1. standard_reference.standard_id 是否真实存在于 KB
    2. cited_excerpt 是否逐字出现在引用的 KB 文档中（含 OCR 容错）
    3. document_position 是否指向审计文档中的真实位置
    """
    ...

class ValidationResult:
    is_valid: bool
    failures: list[str]  # 描述哪些检查失败了
```

验证失败时：重试一次 LLM 调用（更严格的 prompt，点名失败的引用），仍失败则降级为 `insufficient_evidence`。

### P2.2 — 自我验证步骤

**文件:** 新建 `services/self_verifier.py`
**工作量:** M

对每个 topic 审核产出的 AuditIssue，追加一个轻量验证调用。

```python
# services/self_verifier.py
def verify_finding(
    finding: AuditIssue,
    source_evidence: str,   # 文档段落 + KB 引用原文
    llm: BaseLLM,
) -> VerificationVerdict:
    """
    Prompt: "以下是一个审核发现及其证据。请判断该发现是否被证据充分支持。
             输出: VERIFIED / UNVERIFIED / CONTRADICTED + 理由"
    
    temperature=0, 使用更便宜的模型（如 gpt-4o-mini）
    """
    ...
```

验证结果附加到 AuditIssue：
```python
# models/audit_task.py - AuditIssue 新增字段
verification: Optional[VerificationResult] = None
# VerificationResult: {verdict: str, reason: str, verified_at: datetime}
```

UNVERIFIED 和 CONTRADICTED 的发现标记为需要人工审核，不自动采纳。

### P2.3 — HyDE 查询转换

**文件:** `services/vector_search.py` 新增函数
**工作量:** M

在 KB 搜索之前，用 LLM 生成"假设的理想标准条款"，用其向量做检索。

```python
def search_with_hyde(
    kb_ids: list[str],
    topic_prompt: str,
    top_k: int = 6,
) -> str:
    """
    1. LLM (temperature=0): "针对以下审核要求，生成一条假设的理想标准条款：{topic_prompt}"
    2. 嵌入假设条款 → 向量搜索
    3. 重排序
    4. 格式化返回
    """
    ...
```

在 `topic_audit.audit_topic()` 中作为 `_search_kb_by_keywords` 的增强路径（feature flag 控制）。

### P2.4 — 渐进式检索（Progressive Retrieval）

**文件:** `services/vector_search.py` 新增函数
**工作量:** M

首次检索后，LLM 识别证据缺口，重新查询补全。

```python
def search_with_gap_filling(
    kb_ids: list[str],
    topic_prompt: str,
    initial_context: str,
    max_rounds: int = 1,
) -> str:
    """
    1. 将初始检索结果 + 审核要求发给 LLM
    2. LLM 识别："我仍需要关于X的信息"
    3. 用识别出的缺口再次检索 KB
    4. 合并结果
    """
    ...
```

### P2.5 — 跨主题矛盾检测

**文件:** 新建 `services/contradiction_detector.py`
**工作量:** M

在所有 topic 审核完成后，比较引用同一文档位置的发现。

```python
def detect_contradictions(issues: list[AuditIssue]) -> list[Contradiction]:
    """
    按 clause_number / document_position 分组
    如果同一位置的两个发现：
    - type 不同 → 冲突
    - severity 评级相反 → 冲突
    - 一个说合规另一个说违规 → 冲突
    """
    ...

class Contradiction(BaseModel):
    issue_a_id: int
    issue_b_id: int
    conflict_description: str
    clause_number: str
```

矛盾的发现标记为需要人工审核。

### P2.6 — LLM 调用信号量限流

**文件:** `services/audit_task_service.py`
**工作量:** XS

8 个主题并行审核 = 8 个并发 LLM 调用。本地 Ollama 可能扛不住。

```python
import threading
_llm_semaphore = threading.Semaphore(4)  # 本地 Ollama 限 4 并发

def _audit_single_topic_with_limit(...):
    with _llm_semaphore:
        return _audit_single_topic(...)
```

### P2.7 — 中途取消检查

**文件:** `services/audit_task_service.py`
**工作量:** S

当前 `cancel_task()` 只改存储中的 status，但 `run_audit()` 从不检查取消信号。

改动：在 topic 循环中，每个 topic 开始前和结束后检查 `task.status == "cancelled"`，如果已取消则保存部分结果并提前返回。

### P2.8 — Benchmark 阈值校准

**文件:** `benchmark/sweeper.py`, `services/vector_search.py`
**工作量:** S

当前 `relevance > 0.35` 阈值未经校准。运行 benchmark sweep：

```bash
uv run python -m benchmark sweep --kb-ids <ids> \
  --param accept_threshold --range 0.1 0.6 --step 0.05
```

取 MRR 最大值的阈值，替换硬编码的 0.35。

---

## Phase 3：评估体系与数据基础（3-6 周）

### P3.1 — 构建领域审核测试集（30-50 case）

**新建文件:** `data/eval/test_cases/`
**工作量:** L（依赖领域专家）

这是整个优化计划的基石。没有 ground truth，所有指标都是代理指标。

每个 case：
```yaml
- id: case_001
  document: "sample_docs/bid_doc_001.pdf"
  kb_ids: ["kb_national_standards"]
  ground_truth:
    findings:
      - clause_number: "4.2"
        type: "compliance"
        severity: "high"
        description: "质保期12个月，低于标准要求的24个月"
        standard_ref: "GB/T XXXX 5.2"
    no_findings_for:
      - "第三章 投标人资格"  # 明确标注哪些部分不应有问题
  expected:  # 检索评估
    relevant_chunks: ["ch_4.2_0", "ch_4.2_1"]
```

需求：2-3 人周的领域专家时间，覆盖 30-50 个真实审核场景。

### P3.2 — 分层评估体系集成

**文件:** 新建 `eval/` 模块，扩展现有 `benchmark/`
**工作量:** M

基于 FutureAGI 的层拆分模型，每个层有独立指标和阈值：

| 层 | 指标 | 阈值 | 评估方式 |
|----|------|:---:|---------|
| 检索 | ClauseRetrieval@k | ≥0.92 | 确定性比对 |
| 检索 | ContextRelevance | ≥0.85 | LLM-as-judge |
| 检索 | ChunkAttribution | ≥0.80 | 确定性比对 |
| 生成 | Groundedness | ≥0.90 | LLM-as-judge |
| 生成 | FactualAccuracy | ≥0.95 | 确定性比对 |
| 生成 | Completeness | ≥0.80 | LLM-as-judge |
| 拒绝 | AnswerRefusal | ≥0.90 | 对拒绝测试集 |
| 引用 | CitationValidity | ≥0.99 | 确定性比对 |

```bash
uv run python -m eval run --test-set data/eval/test_cases/ --kb-ids <ids>
```

输出：每层每个指标的得分，低于阈值标红。

### P3.3 — RAGAS 生成质量评估

**文件:** 扩展 `scripts/eval_qa.py`
**工作量:** M

集成 `ragas` 库，评估 faithfulness、answer relevancy、context precision、context recall。

```bash
uv add ragas
uv run python scripts/eval_qa.py --kb-ids <ids> --eval-mode ragas
```

### P3.4 — LLM-as-Judge 周期性评估

**文件:** 新建 `eval/judge.py`
**工作量:** M

用 GPT-4o-mini 做 judge，每周跑一次：
- 所有发现是否被证据支持？
- 是否有明显遗漏的问题？
- 引用是否准确？

结果输出为趋势报告，追踪质量变化。

### P3.5 — CI 质量门禁

**文件:** 新建 `scripts/ci_benchmark.py`
**工作量:** S

```bash
uv run python scripts/ci_benchmark.py --threshold-mrr 0.7
```

MRR 低于阈值 → CI 失败。防止检索退化不被发现。

---

## Phase 4：架构加固（6-12 周）

### P4.1 — FAISS IndexIDMap 实现向量级删除

**文件:** `core/index_manager.py`
**工作量:** S

当前 `remove_document` 走全量重建（因为 HNSW 不支持 `remove_ids`）。修复：
- 用 `faiss.IndexIDMap(hnsw_index)` 包裹 HNSW 索引
- 覆写 LlamaIndex `FaissVectorStore.add()` → `add_with_ids()`
- 实现 `remove_ids()` 快速路径

效果：删除文档从 O(n) 降至 O(1)。

### P4.2 — 替换 daemon 线程为 ARQ 任务队列

**文件:** `services/audit_task_service.py`, 新建 `worker/`
**工作量:** L

`threading.Thread(daemon=True)` 的问题：服务重启强杀，任务丢失，无重试，无持久化。

```bash
uv add arq
```

- 新建 `worker/tasks.py`：ARQ 任务定义
- 新建 `worker/main.py`：ARQ worker 入口
- 修改 `run_audit_async()`：`arq.enqueue_job()` 替代 `Thread.start()`
- 需要 Redis（`docker-compose.yml` 增加 redis 服务）

### P4.3 — W3C PROV 审计追踪

**文件:** 新建 `services/audit_trail.py`
**工作量:** M

不可变审计日志，记录每次审核的完整上下文：

```python
class AuditTrailEntry(BaseModel):
    task_id: str
    timestamp: datetime
    event: str                     # "audit_started", "topic_completed", ...
    model_version: str             # LLM 模型标识
    prompt_hash: str               # prompt 模板的 hash
    input_hashes: dict[str, str]   # 输入数据的 hash
    output_hash: str               # 输出的 hash
    degradation_log: list[dict]    # 降级记录
```

支持 N-1 回溯：用相同的 model + prompt + input → 应得到相同结果。

### P4.4 — LLM 可观测性（Langfuse 追踪）

**工作量:** M

```bash
uv add langfuse
```

在 `core/settings.py` 的 `get_llm()` 中集成 Langfuse callback，追踪每次 LLM 调用的：
- 延迟、token 用量、cost
- 成功/失败/重试
- prompt 和 response 内容

目的：Nebius 发现 27% 的静默失败率（sub-agent hit max_tokens、返回空消息）。没有追踪就无法发现。

### P4.5 — 长文档两轮选题

**文件:** `services/agent_audit.py`
**工作量:** M

当前 topic selection 只能看到文档前 8000 字符。长文档改进：

```
Pass 1: 将 structure_service 提取的目录结构发给 LLM
        → 识别哪些章与审核相关
Pass 2: 将相关章节的完整文本发给 LLM
        → 详细分析，确定具体审核主题
```

当 `len(parsed_content) < 20000` 时走原路径（全文直送），超过则走两轮路径。

---

## Phase 5：模型升级与前沿（Backlog，按需启动）

### P5.1 — 评估 Qwen3-Embedding-4B 替代 BGE-M3

**触发条件:** Phase 3 领域测试集建立之后
**工作量:** L

- 在领域测试集上对比 BGE-M3 vs Qwen3-Embedding-4B
- 关键问题：BGE-M3 的稀疏输出在标准号/条款引用上的优势是否被 Qwen3 的整体精度提升超越
- 如果切换：全量重建索引，修改 settings.py embedding 加载逻辑

### P5.2 — 微调领域审核模型

**触发条件:** 积累 500+ expert-reviewed audit 样本后
**工作量:** L

- 基座模型：Qwen 2.5 7B 或 32B
- 微调方式：QLoRA（内存友好）
- 目标：per-topic audit 角色，替代通用 LLM
- 预期收益：+5-15pp recall，~100ms 延迟，几分钱一次

### P5.3 — Late Chunking / ColBERT 端到端检索

**触发条件:** 发现文档中代词/交叉引用（"上述要求""前述条款"）导致检索失败比例高
**工作量:** L

- Jina AI Late Chunking (2025)：全文 embed → 池化连续 span
- 对 anaphora 密集型查询有 +29% 相似度提升

### P5.4 — Graph RAG 跨文档分析

**触发条件:** 需要跨多个标准文档做多跳推理
**工作量:** L

- 从 KB 文档中抽取实体关系构建知识图谱
- 支持查询："引用 GB/T XXXX 5.2 的所有相关标准"

### P5.5 — 对抗辩论验证（高冲突发现）

**触发条件:** 批评 Agent 标记为 CONTRADICTED 的发现比例 > 5%
**工作量:** M

- 两个 Agent 各自论证支持和反对
- 第三个 Agent 裁决
- 仅应用于被标记为矛盾的少量发现（控制成本）

---

## 风险矩阵

| 阶段 | 最大风险 | 缓解措施 |
|------|---------|---------|
| Phase 0 | 强制引用导致本地 Ollama 产出率下降 → 发现变少 | 先用 soft enforcement（缺少引用标记低置信度），收集数据后再收紧 |
| Phase 1 | 需求提取质量不稳定 → 审核基础不可靠 | Phase 0 离线预处理 + 人工审核界面，需求入库前必须人工确认 |
| Phase 1 | 新老管线并存增加复杂度 | feature flag 控制，默认走旧管线，新管线 opt-in |
| Phase 2 | 验证步骤加倍 LLM 调用量 → 延迟和成本翻倍 | 验证用更便宜的模型（gpt-4o-mini），确定性检查优先于 LLM 检查 |
| Phase 3 | 领域专家带宽不足 → 测试集迟迟建不起来 | 即使只有 5-10 个 case 也比零好，可以渐进积累 |
| Phase 4 | ARQ 迁移复杂 → 影响稳定性 | 可以和 daemon thread 并行运行，逐步切换 |
| Phase 5 | 模型升级 ROI 不确定 | 必须在领域测试集上验证后再切换，不跟风 benchmark |

---

## 附录 A：所有硬编码魔数清单（待校准）

| 位置 | 变量/值 | 含义 | 校准方式 |
|------|---------|------|---------|
| `services/vector_search.py:187` | `0.35` | 向量搜索接受阈值 | benchmark sweep (P2.8) |
| `core/settings.py:38` | `chunk_size=512` | 分块大小 | benchmark sweep chunk_size |
| `core/settings.py:39` | `chunk_overlap=50` | 分块重叠 | benchmark sweep overlap |
| `services/topic_audit.py:76` | `1500` | 关键词上下文窗口 | P1.3 后按文档长度自适应 |
| `services/agent_audit.py:56` | `8000` | Agent 文档预览长度 | P4.5 后按文档长度自适应 |
| `core/settings.py:93` | `max_length=512` | embedding 最大长度 | 评估 Late Chunking 后调整 |
| `core/settings.py:95` | `embed_batch_size=2` | embedding 批大小 | 基准测试不同 batch size 的吞吐/内存 |
| `core/index_manager.py:68-69` | `efConstruction=200, efSearch=64` | HNSW 参数 | 对小型 KB 考虑 FlatIP (P1.2 附录) |

## 附录 B：Quick Wins 汇总（无需等待，今天就能做）

1. `temperature=0` for all audit LLM calls → `core/settings.py` 一行
2. `^#{1,6}` heading regex → `core/index_manager.py` 一行
3. `top_k=6` for KB search → `services/vector_search.py` 一行
4. `logger.warning` for rga not found → `services/vector_search.py` 一行
5. `insufficient_evidence` + `out_of_scope` type enum → 两个文件各加两个值
6. `LLM call semaphore` → `services/audit_task_service.py` 五行
7. `cancel check` in topic loop → `services/audit_task_service.py` 一个 if 判断
