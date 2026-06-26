# 审核结果标准依据 PDF 跳转定位 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 审核结果中的标准依据可点击，新标签页打开 PDF 预览并定位到条款所在页，高亮条款文字

**Architecture:** 数据链路改造 — import 时逐页提取 PDF 文本存入 metadata → 索引时按页创建 chunk 继承 page_number → search_kb 返回带 doc_id/page_number 的结构化信息 → LLM flag_issue 记录溯源字段 → API 响应补齐新字段 → 前端新增 /pdf-viewer 路由用 pdfjs-dist 渲染 PDF 并跳转高亮

**Tech Stack:** pdfplumber (已有), pdfjs-dist (前端新增), FAISS/LlamaIndex (已有), React Router

## Global Constraints

- 所有新增字段均为 optional，LLM 遗漏时不阻塞审核
- PDF 文件服务端点必须支持 Range 请求（pdfjs 需要按 range 分片加载）
- doc_id / file_path 需做路径穿越校验
- 老文档通过 `cli kb reindex --doc-id <id>` 重建索引以获取页码
- DOCX/MD 格式仅做纯文本降级预览

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `core/text_extraction.py` | 新增逐页 PDF 文本提取函数 | 修改 |
| `services/doc_service.py` | import 时存储 page_texts，传入索引器 | 修改 |
| `core/index_manager.py` | 按页创建 Document chunk，写入 page_number metadata | 修改 |
| `models/audit_task.py` | StandardRef 新增溯源字段 | 修改 |
| `models/llm_schemas.py` | AgentAction 新增 flag_issue 溯源参数 | 修改 |
| `services/agentic_audit.py` | search_kb 返回增强、flag_issue 溯源、系统提示词更新、issue_found 事件补齐 | 修改 |
| `api/routers/kb_files.py` | PDF 文件服务 + 单文档元数据 + 页面文本提取端点 | 新建 |
| `api/main.py` | 注册 kb_files 路由 | 修改 |
| `api/routers/audit_tasks.py` | IssueResponse 补齐新字段 | 修改 |
| `storage/doc_repo.py` | 新增 find_doc_by_id 全局查询 | 修改 |
| `cli/main.py` | index rebuild 命令新增 --doc-id 参数 | 修改 |
| `frontend/src/api/types.ts` | 同步新增字段 | 修改 |
| `frontend/src/pages/PdfViewer.tsx` | PDF 查看器页面 | 新建 |
| `frontend/src/App.tsx` | 注册 /pdf-viewer/:docId 路由 | 修改 |
| `frontend/src/pages/AuditResult.tsx` | 标准依据链接渲染 | 修改 |
| `frontend/src/components/AuditStream.tsx` | 流式 IssueCard 标准依据链接 + issue_found 事件字段 | 修改 |
| `frontend/package.json` | 新增 pdfjs-dist | 修改 |

---

### Task 1: 逐页 PDF 文本提取

**Files:**
- Modify: `core/text_extraction.py`

**Interfaces:**
- Produces: `extract_text_by_page(file_path: str) -> list[tuple[int, str]]` — 返回 `[(page_num_0based, text), ...]`
- Produces: `extract_text(file_path: str) -> str` — 现有函数保留不变，内部委托给新函数再拼接

- [ ] **Step 1: 读取现有 text_extraction.py**

```bash
# 确认现有 extract_text 实现
```

- [ ] **Step 2: 新增 extract_text_by_page 函数并重构 extract_text**

在 `core/text_extraction.py` 中：

```python
def extract_text_by_page(file_path: str) -> list[tuple[int, str]]:
    """逐页提取 PDF 文本，返回 [(页码_0based, 文本), ...]。
    
    非 PDF 文件返回 [(0, full_text)]，页码为虚拟页码。
    """
    import pdfplumber
    from pathlib import Path
    
    ext = Path(file_path).suffix.lower()
    
    if ext == '.pdf':
        pages = []
        try:
            with pdfplumber.open(file_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append((i, text))
            if not pages:
                # 所有页面都为空，返回空文本
                return [(0, "")]
            return pages
        except Exception:
            pass
    
    # 非 PDF：整个文本作为一页
    text = extract_text(file_path)
    return [(0, text)] if text else []


# 保留原有 extract_text 函数不动，
# 但内部可以调用 extract_text_by_page 再拼接（可选优化）
```

- [ ] **Step 3: 验证**

```bash
uv run python -c "
from core.text_extraction import extract_text_by_page
pages = extract_text_by_page('sample_docs/sample.pdf')
print(f'Pages: {len(pages)}')
for i, (pn, text) in enumerate(pages[:3]):
    print(f'  Page {pn}: {len(text)} chars, preview: {text[:80]}...')
"
```

- [ ] **Step 4: Commit**

```bash
git add core/text_extraction.py
git commit -m "feat: add extract_text_by_page for page-aware PDF text extraction"
```

---

### Task 2: 存储 page_texts 到 doc metadata 并传入索引器

**Files:**
- Modify: `services/doc_service.py:57-126` (import_document 函数)
- Modify: `services/vector_search.py:172-181` (index_document 函数)

**Interfaces:**
- Consumes: `extract_text_by_page(file_path) -> list[tuple[int, str]]` (from Task 1)
- Produces: `doc.metadata["page_texts"]` — list of page text strings stored in doc metadata
- Produces: `vector_search.index_document(kb_id, doc_id, file_path, source_name, page_texts=None)` — 新增可选参数

- [ ] **Step 1: 修改 doc_service.import_document() — 存储 page_texts**

在 `services/doc_service.py` 的 `import_document()` 函数中，pdfplumber 打开文件提取页数的位置（约第 92-102 行），扩展为同时提取逐页文本：

```python
# 原代码（约 line 92-102）:
if file_type == "pdf":
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        with pdfplumber.open(tmp_path) as pdf:
            doc.page_count = len(pdf.pages)
        os.unlink(tmp_path)
    except Exception as e:
        _logger.warning("failed to extract page count for %s: %s", doc.id, e)

# 改为:
if file_type == "pdf":
    try:
        import tempfile
        from core.text_extraction import extract_text_by_page
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        with pdfplumber.open(tmp_path) as pdf:
            doc.page_count = len(pdf.pages)
        # 提取逐页文本并存储到 metadata
        page_texts = extract_text_by_page(tmp_path)
        doc.metadata["page_texts"] = [text for _, text in page_texts]
        os.unlink(tmp_path)
    except Exception as e:
        _logger.warning("failed to extract page data for %s: %s", doc.id, e)
```

- [ ] **Step 2: 修改 doc_service.import_document() — 传递 page_texts 给索引器**

在同一函数的索引调用处（约第 108-126 行），将 page_texts 传入：

```python
# 原代码 (line 107-126):
# 向量索引
if doc.file_path:
    if async_index:
        doc.index_status = "pending_index"
        doc_repo._save_doc_meta(doc)
        thread = threading.Thread(
            target=_index_single_doc_async,
            args=(kb_id, doc),
            daemon=True,
        )
        thread.start()
    else:
        try:
            _index_vec(kb_id, doc.id, doc.file_path)
        except Exception as e:
            _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)

# 改为:
if doc.file_path:
    page_texts = doc.metadata.get("page_texts")
    if async_index:
        doc.index_status = "pending_index"
        doc_repo._save_doc_meta(doc)
        thread = threading.Thread(
            target=_index_single_doc_async,
            args=(kb_id, doc),
            daemon=True,
        )
        thread.start()
    else:
        try:
            _index_vec(kb_id, doc.id, doc.file_path, page_texts=page_texts)
        except Exception as e:
            _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)
```

- [ ] **Step 3: 修改 _index_single_doc_async — 传递 page_texts**

```python
# 在 _index_single_doc_async 函数中 (约 line 148):
# 原: _index_vec(kb_id, doc.id, doc.file_path)
# 改为:
page_texts = doc.metadata.get("page_texts")
_index_vec(kb_id, doc.id, doc.file_path, page_texts=page_texts)
```

- [ ] **Step 4: 修改 vector_search.index_document() — 接受 page_texts 参数**

在 `services/vector_search.py` 的 `index_document()` 函数中：

```python
# 原函数签名:
def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = ""):

# 改为:
def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = "", page_texts: list[str] | None = None):
    """对单篇 KB 文档分块 + embedding 并写入 FAISS 索引。"""
    text = _extract_text(file_path)
    if not text or len(text) < 20:
        return
    source_name = source_name or Path(file_path).stem
    _index_to_store(kb_id, doc_id, text, source_name, page_texts=page_texts)
```

- [ ] **Step 5: 同样修改 _batch_index_docs 中的索引调用**

在 `services/doc_service.py` 的 `_batch_index_docs()` 函数中（约 line 250），`texts.append((doc.id, text, doc.original_name))` 改为也携带 page_texts：

```python
# 原:
texts.append((doc.id, text, doc.original_name))

# 改为:
page_texts = doc.metadata.get("page_texts")
texts.append((doc.id, text, doc.original_name, page_texts))
```

对应地修改 `index_documents_batch` 的调用者（同一函数中）来解包 4 元组。

- [ ] **Step 6: 验证**

```bash
uv run python -c "
from services.doc_service import import_document
# 导入一个 PDF 到测试 KB，验证 doc.metadata['page_texts'] 存在
"
```

- [ ] **Step 7: Commit**

```bash
git add services/doc_service.py services/vector_search.py
git commit -m "feat: store page_texts in doc metadata and pass to indexer"
```

---

### Task 3: index_manager 按页创建 chunk 并写入 page_number

**Files:**
- Modify: `core/index_manager.py:207-253` (index_document 函数)
- Modify: `core/index_manager.py:256-288` (_enrich_chunk_metadata 函数)
- Modify: `core/index_manager.py:290-358` (index_documents_batch 函数)

**Interfaces:**
- Consumes: `page_texts: list[str] | None` — from Task 2
- Produces: `node.metadata["page_number"]` — int (0-based page number)
- Produces: `node.metadata["doc_id"]` — already exists, ensure it's always set

- [ ] **Step 1: 修改 index_document() — 接受 page_texts 并按页创建 Document**

```python
# 修改函数签名:
def index_document(kb_id: str, doc_id: str, text: str, source_name: str = "",
                   page_texts: list[str] | None = None):
    """对文档文本分块 → embedding → 写入 KB 索引 + 持久化向量。
    
    若提供 page_texts（逐页文本列表），每页独立创建 Document，
    分块后各 node 自动继承对应页码。
    """
    if not text or len(text) < 20:
        return

    with _get_index_lock(kb_id):
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index document")

        index = get_kb_index(kb_id)

        all_nodes = []
        all_embeddings = []

        if page_texts and len(page_texts) > 0:
            # 按页创建 Document，每页生成独立 chunks
            for page_num, page_text in enumerate(page_texts):
                if not page_text or len(page_text.strip()) < 10:
                    continue
                doc = Document(
                    text=page_text,
                    id_=f"{doc_id}_p{page_num}",
                    metadata={
                        "doc_id": doc_id,
                        "source": source_name or doc_id,
                        "page_number": page_num,  # 0-based
                    },
                )
                nodes = _split_document(doc)
                for node in nodes:
                    node.metadata["page_number"] = page_num
                _enrich_chunk_metadata(nodes, doc_id, source_name or doc_id)
                all_nodes.extend(nodes)
                del doc
        else:
            # 无 page_texts：整体文本作为一个 Document（兼容老数据）
            doc = Document(
                text=text,
                id_=doc_id,
                metadata={"doc_id": doc_id, "source": source_name or doc_id},
            )
            nodes = _split_document(doc)
            _enrich_chunk_metadata(nodes, doc_id, source_name or doc_id)
            all_nodes = nodes
            del doc

        if not all_nodes:
            return

        # 批量 embedding
        node_texts = [node.text or "" for node in all_nodes]
        with get_gpu_inference_lock():
            embeddings = _embed_batch_with_retry(embed_model, node_texts)
        for node, emb in zip(all_nodes, embeddings):
            node.embedding = emb

        # 持久化向量
        _save_doc_vectors(kb_id, doc_id, all_nodes, embeddings)

        # 插入索引
        index.insert_nodes(all_nodes)

        del all_nodes, embeddings
        gc.collect()

        _persist(kb_id, index)
```

- [ ] **Step 2: 修改 index_documents_batch() — 同样支持 page_texts**

修改 `index_documents_batch` 的签名和内部逻辑，接受 4 元组 `(doc_id, text, source_name, page_texts)`：

```python
def index_documents_batch(
    kb_id: str,
    docs: list[tuple[str, str, str, list[str] | None]],  # 改为4元组
    progress_callback=None,
):
    # ... 内部遍历改为:
    for i, (doc_id, text, source_name, page_texts) in enumerate(docs, 1):
        # ... 在创建 Document 时:
        if page_texts and len(page_texts) > 0:
            for page_num, page_text in enumerate(page_texts):
                if not page_text or len(page_text.strip()) < 10:
                    continue
                doc = Document(
                    text=page_text,
                    id_=f"{doc_id}_p{page_num}",
                    metadata={
                        "doc_id": doc_id,
                        "source": source_name or doc_id,
                        "page_number": page_num,
                    },
                )
                nodes = _split_document(doc)
                for node in nodes:
                    node.metadata["page_number"] = page_num
                # ...
```

- [ ] **Step 3: 确保 search() 函数返回 page_number**

在 `core/index_manager.py` 的 `search()` 函数中（约 line 639-652），确认 `page_number` 已经在 hits dict 中返回：

```python
# 现有的 hits 构建（约 line 639-652）:
hits.append({
    "source": "vec_search",
    "kb_id": meta.get("kb_id", ""),
    "doc_id": meta.get("doc_id", ""),
    "content": node.text,
    "doc_source": meta.get("source", ""),
    "section_path": meta.get("section_path", ""),
    "clause_number": meta.get("clause_number", ""),
    "relevance": round(node.get_score() or 0, 4),
    # 新增:
    "page_number": meta.get("page_number"),  # int or None
})
```

- [ ] **Step 4: 验证**

```bash
# 导入 PDF 后验证 search 结果含 page_number
uv run python -c "
from core.index_manager import search
results = search(['<kb_id>'], 'test query')
for r in results:
    print(f'doc_id={r[\"doc_id\"]}, page_number={r.get(\"page_number\")}')
"
```

- [ ] **Step 5: Commit**

```bash
git add core/index_manager.py
git commit -m "feat: page-aware indexing — per-page Document chunks with page_number metadata"
```

---

### Task 4: 数据模型变更

**Files:**
- Modify: `models/audit_task.py:18-23` (StandardRef)
- Modify: `models/llm_schemas.py:69-185` (AgentAction)

**Interfaces:**
- Produces: `StandardRef.doc_id`, `StandardRef.page_number`, `StandardRef.chunk_text`
- Produces: `AgentAction.standard_doc_id`, `AgentAction.standard_page_number`, `AgentAction.standard_chunk_text`

- [ ] **Step 1: 修改 StandardRef**

在 `models/audit_task.py` 的 `StandardRef` 类中新增字段：

```python
class StandardRef(BaseModel):
    """标准依据"""
    standard_name: str
    standard_id: str
    clause: Optional[str] = None
    requirement: Optional[str] = None
    # 新增 — PDF 跳转溯源
    doc_id: Optional[str] = None          # KB 文档 ID，定位文件
    page_number: Optional[int] = None     # 条款所在页码 (0-based)
    chunk_text: Optional[str] = None      # chunk 原文片段，用于 PDF 高亮搜索
```

- [ ] **Step 2: 修改 AgentAction — 新增 flag_issue 溯源参数**

在 `models/llm_schemas.py` 的 `AgentAction` 类中，在 `issue_suggestion` 字段之后新增三个字段：

```python
    # — flag_issue 溯源参数（新增）—
    standard_doc_id: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准文档的 ID，必须来自 search_kb 返回结果中的 doc_id 字段。"
            "示例：'01J...'（ULID 格式）。"
        ),
    )
    standard_page_number: Optional[int] = Field(
        default=None,
        description=(
            "flag_issue: 标准条款所在页码，必须来自 search_kb 返回结果中的 page_number 字段。"
            "从1开始计数。"
        ),
    )
    standard_chunk_text: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准条款的原文片段，来自 search_kb 返回结果中的内容。"
            "用于在 PDF 中定位和高亮具体条款文字。"
        ),
    )
```

- [ ] **Step 3: 验证模型可正常序列化/反序列化**

```bash
uv run python -c "
from models.audit_task import StandardRef
s = StandardRef(standard_name='test', standard_id='test', doc_id='01J123', page_number=5, chunk_text='hello')
print(s.model_dump())
from models.llm_schemas import AgentAction
a = AgentAction(thought='test', action='flag_issue', standard_doc_id='01J123')
print(a.standard_doc_id)
"
```

- [ ] **Step 4: Commit**

```bash
git add models/audit_task.py models/llm_schemas.py
git commit -m "feat: add traceability fields to StandardRef and AgentAction for PDF jump"
```

---

### Task 5: search_kb 返回增强 + flag_issue 溯源 + 系统提示词

**Files:**
- Modify: `services/agentic_audit.py:271-311` (_tool_search_kb)
- Modify: `services/agentic_audit.py:346-380` (_tool_flag_issue)
- Modify: `services/agentic_audit.py:77-120` (SYSTEM_PROMPT)
- Modify: `services/agentic_audit.py:774-808` (NATIVE_SYSTEM_PROMPT)
- Modify: `services/agentic_audit.py:678-771` (_TOOLS_SPEC flag_issue 参数)
- Modify: `services/agentic_audit.py:811-853` (_execute_native_tool flag_issue 分支)
- Modify: `services/agentic_audit.py:1012-1025` (issue_found 事件)

**Interfaces:**
- Consumes: search results from `index_manager.search()` now includes `page_number`, `doc_id`
- Produces: search result text includes `doc_id` and `page_number` lines for LLM
- Produces: `_tool_flag_issue` writes `doc_id`, `page_number`, `chunk_text` to StandardRef
- Produces: issue_found SSE event includes new fields

- [ ] **Step 1: 修改 _tool_search_kb() — 返回结果增加 doc_id 和页码**

在 `services/agentic_audit.py` 的 `_tool_search_kb()` 函数中：

```python
# 原代码 (约 line 293-311):
lines = [f"【知识库搜索结果（搜索词: {query}，共 {len(results)} 条）】"]
for i, r in enumerate(results, 1):
    relevance = r.get("relevance", 0)
    doc = r.get("doc_source", "") or r.get("doc_id", "")
    clause = r.get("clause_number", "")
    section = r.get("section_path", "")
    content = (r.get("content", "") or "")

    label_parts = []
    if doc:
        label_parts.append(f"【{doc}】")
    if clause:
        label_parts.append(f"第{clause}条")
    if section and not clause:
        label_parts.append(section)
    label = " ".join(label_parts) if label_parts else "未知来源"

    lines.append(f"\n{i}. {label} (相关度: {relevance:.2f})\n   {content}")
return "\n".join(lines)

# 改为:
lines = [f"【知识库搜索结果（搜索词: {query}，共 {len(results)} 条）】"]
for i, r in enumerate(results, 1):
    relevance = r.get("relevance", 0)
    doc = r.get("doc_source", "") or r.get("doc_id", "")
    doc_id = r.get("doc_id", "")
    clause = r.get("clause_number", "")
    section = r.get("section_path", "")
    page_number = r.get("page_number")  # 新增
    content = (r.get("content", "") or "")

    label_parts = []
    if doc:
        label_parts.append(f"【{doc}】")
    if clause:
        label_parts.append(f"第{clause}条")
    if section and not clause:
        label_parts.append(section)
    label = " ".join(label_parts) if label_parts else "未知来源"

    # 新增 meta 行
    meta_parts = [f"相关度: {relevance:.2f}"]
    if doc_id:
        meta_parts.append(f"doc_id: {doc_id}")
    if page_number is not None:
        meta_parts.append(f"页码: 第{page_number + 1}页")  # 0-based → 1-based
    meta_line = " | ".join(meta_parts)

    lines.append(f"\n{i}. {label}\n   {meta_line}\n   {content}")
return "\n".join(lines)
```

- [ ] **Step 2: 修改 _tool_flag_issue() — 写入溯源字段**

```python
# 修改 _tool_flag_issue 中的 StandardRef 构造 (约 line 361-366):
standard_reference=StandardRef(
    standard_name=action.standard_name or "",
    standard_id=action.standard_name or "",
    clause=action.standard_clause,
    requirement=action.standard_requirement,
    # 新增溯源字段
    doc_id=action.standard_doc_id,
    page_number=action.standard_page_number,
    chunk_text=action.standard_chunk_text,
),
```

- [ ] **Step 3: 修改 _TOOLS_SPEC 中 flag_issue 的参数定义**

在 `_TOOLS_SPEC` 的 flag_issue function parameters 中（约 line 695-771），在 `suggestion` 字段之后新增三个参数定义：

```python
{
    "type": "function",
    "function": {
        "name": "flag_issue",
        "description": (
            # ... 现有 description，在末尾增加:
            "新增可选的溯源参数 standard_doc_id、standard_page_number、standard_chunk_text，"
            "用于在审核结果中生成可点击跳转到标准 PDF 原文的链接。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                # ... 现有参数 ...
                "suggestion": {
                    "type": "string",
                    "description": "...",
                },
                # 新增 ——
                "standard_doc_id": {
                    "type": "string",
                    "description": (
                        "标准文档的 ID，从 search_kb 返回结果的 doc_id 字段获取。"
                        "可选，但强烈建议提供——使审核结果可跳转到标准 PDF 原文。"
                    ),
                },
                "standard_page_number": {
                    "type": "integer",
                    "description": (
                        "标准条款所在页码，从 search_kb 返回结果的页码字段获取。"
                        "从1开始计数。可选。"
                    ),
                },
                "standard_chunk_text": {
                    "type": "string",
                    "description": (
                        "标准条款的原文片段，从 search_kb 返回的内容中摘录。"
                        "可选，用于在 PDF 中高亮定位。"
                    ),
                },
            },
            "required": ["issue_type", "severity", "description", "cited_excerpt"],
        },
    },
},
```

- [ ] **Step 4: 修改 _execute_native_tool() — flag_issue 分支传递新参数**

在 `_execute_native_tool()` 函数中（约 line 834-848），flag_issue 分支新增参数：

```python
elif func_name == "flag_issue":
    action = AgentAction(
        thought="",
        action="flag_issue",
        issue_type=args.get("issue_type"),
        issue_severity=args.get("severity"),
        issue_description=args.get("description"),
        standard_name=args.get("standard_name"),
        standard_clause=args.get("standard_clause"),
        standard_requirement=args.get("standard_requirement"),
        cited_excerpt=args.get("cited_excerpt"),
        document_position=args.get("document_position"),
        issue_suggestion=args.get("suggestion"),
        # 新增
        standard_doc_id=args.get("standard_doc_id"),
        standard_page_number=args.get("standard_page_number"),
        standard_chunk_text=args.get("standard_chunk_text"),
    )
    return _tool_flag_issue(action, issues)
```

- [ ] **Step 5: 修改 issue_found SSE 事件 — 补齐溯源字段**

在两处发送 issue_found 事件的地方（native 路径约 line 1012-1025，structured_llm 路径约 line 1193-1206），新增字段：

```python
# native 路径 (约 line 1014-1024):
_emit({
    "type": "issue_found",
    "issue": {
        "id": new_issue.id,
        "type": new_issue.type,
        "severity": new_issue.severity,
        "description": new_issue.description[:300],
        "standard_name": new_issue.standard_reference.standard_name if new_issue.standard_reference else None,
        "standard_clause": new_issue.standard_reference.clause if new_issue.standard_reference else None,
        # 新增
        "standard_doc_id": new_issue.standard_reference.doc_id if new_issue.standard_reference else None,
        "standard_page_number": new_issue.standard_reference.page_number if new_issue.standard_reference else None,
        "standard_chunk_text": new_issue.standard_reference.chunk_text if new_issue.standard_reference else None,
    },
})

# structured_llm 路径同理 (约 line 1196-1205)
```

- [ ] **Step 6: 更新系统提示词**

在 `SYSTEM_PROMPT`（约 line 77-120）的 "flag_issue 要求" 段落增加：

```python
SYSTEM_PROMPT = """...
## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自 search_kb 的返回结果
- document_position 必须使用文档中的实际章节名称，不要使用编号
- description 清晰说明问题和标准依据
- 建议同时提供 suggestion（修改建议）
- 建议提供 standard_doc_id、standard_page_number、standard_chunk_text，
  这些值来自 search_kb 返回结果中的 doc_id、页码、内容字段，
  用于在审核报告中生成可点击跳转到标准 PDF 原文的链接
..."""
```

同样在 `NATIVE_SYSTEM_PROMPT`（约 line 774-808）的 "flag_issue 要求" 段落增加：

```python
NATIVE_SYSTEM_PROMPT = """...
## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自搜索工具的返回结果
- document_position 必须使用文档中的实际章节名称，不要使用编号
- description 清晰说明问题和标准依据
- 建议提供 standard_doc_id、standard_page_number、standard_chunk_text
  来自搜索工具返回结果中的 doc_id、页码、内容字段
..."""
```

- [ ] **Step 7: 验证**

```bash
uv run python -c "
from services.agentic_audit import _tool_search_kb
# 模拟测试 search_kb 返回格式含 doc_id 和 page_number
"
```

- [ ] **Step 8: Commit**

```bash
git add services/agentic_audit.py
git commit -m "feat: enhance search_kb results with doc_id/page_number, flag_issue with traceability"
```

---

### Task 6: 新增 API 端点（kb_files 路由）

**Files:**
- Create: `api/routers/kb_files.py`
- Modify: `api/main.py:93-99` (注册路由)
- Modify: `storage/doc_repo.py` (新增 find_doc_by_id)

**Interfaces:**
- Produces: `GET /api/v1/kb-documents/{doc_id}` — 单文档元数据
- Produces: `GET /api/v1/kb-documents/{doc_id}/file` — 原始文件（支持 Range）
- Produces: `GET /api/v1/kb-documents/{doc_id}/page/{page_number}` — 页面文本

- [ ] **Step 1: 新增 doc_repo.find_doc_by_id()**

在 `storage/doc_repo.py` 末尾新增：

```python
def find_doc_by_id(doc_id: str) -> Optional[KBDocument]:
    """跨所有 KB 查找指定 ID 的文档。
    
    扫描 data/kbs/ 下所有 KB 目录的 meta 文件。
    doc_id 会被校验防止路径穿越。
    """
    validate_id(doc_id, "doc_id")
    for kb_dir in DATA_DIR.glob("kbs/*"):
        if not kb_dir.is_dir():
            continue
        kb_id = kb_dir.name
        doc = get_doc(kb_id, doc_id)
        if doc:
            return doc
    return None
```

- [ ] **Step 2: 创建 api/routers/kb_files.py**

```python
"""KB 文档文件服务 — PDF 预览、文本降级、元数据查询。"""

import os
import mimetypes
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse

import storage.doc_repo as doc_repo
from storage import validate_id

router = APIRouter(prefix="/api/v1/kb-documents", tags=["kb-documents"])


@router.get("/{doc_id}")
def get_document_meta(doc_id: str):
    """获取单个 KB 文档的元数据（供 PDF 查看器使用）。"""
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "id": doc.id,
        "name": doc.name,
        "original_name": doc.original_name,
        "file_type": doc.file_type,
        "page_count": doc.page_count,
        "kb_id": doc.kb_id,
    }


@router.get("/{doc_id}/file")
def get_document_file(doc_id: str, request: Request):
    """返回 KB 文档的原始文件。支持 Range 请求（pdfjs 需要）。"""
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    file_path = Path(doc.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 路径穿越校验：确保文件在 data/kbs/ 目录下
    data_dir = Path(os.environ.get("AUDIT_DATA_DIR", "./data")).resolve()
    try:
        file_path.resolve().relative_to(data_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法文件路径")

    # Content-Type
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    file_size = file_path.stat().st_size

    # Range 请求支持
    range_header = request.headers.get("range")
    if range_header:
        import re
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end_str = match.group(2)
            end = int(end_str) if end_str else file_size - 1

            if start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")

            def range_stream():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        data = f.read(min(chunk_size, remaining))
                        if not data:
                            break
                        yield data
                        remaining -= len(data)

            return StreamingResponse(
                range_stream(),
                status_code=206,
                media_type=media_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(end - start + 1),
                },
            )

    # 非 Range 请求：流式返回整个文件
    def full_stream():
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        full_stream(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )


@router.get("/{doc_id}/page/{page_number}")
def get_page_text(doc_id: str, page_number: int):
    """获取文档指定页码的文本内容（非 PDF 格式的降级预览）。"""
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    page_texts = doc.metadata.get("page_texts")
    if not page_texts:
        raise HTTPException(status_code=404, detail="该文档无逐页文本数据")

    if page_number < 0 or page_number >= len(page_texts):
        raise HTTPException(status_code=404, detail=f"页码 {page_number} 超出范围 (0-{len(page_texts)-1})")

    return {
        "page_number": page_number,
        "text": page_texts[page_number],
        "total_pages": len(page_texts),
    }
```

- [ ] **Step 3: 注册路由到 main.py**

在 `api/main.py` 中：

```python
# 在 import 部分增加:
from api.routers import kb_files

# 在路由注册部分增加:
app.include_router(kb_files.router)
```

- [ ] **Step 4: 验证**

```bash
# 启动后端
uv run uvicorn api.main:app --port 8000 &
sleep 3
# 测试元数据端点
curl http://localhost:8000/api/v1/kb-documents/<doc_id>
# 测试文件端点（验证 Range 支持）
curl -I -H "Range: bytes=0-1023" http://localhost:8000/api/v1/kb-documents/<doc_id>/file
# 杀掉后端
pkill -f "uvicorn api.main"
```

- [ ] **Step 5: Commit**

```bash
git add api/routers/kb_files.py api/main.py storage/doc_repo.py
git commit -m "feat: add KB document file serving endpoints with Range support"
```

---

### Task 7: IssueResponse 更新

**Files:**
- Modify: `api/routers/audit_tasks.py:47-55` (IssueResponse)
- Modify: `api/routers/audit_tasks.py:137-149` (构建 IssueResponse 的逻辑)

**Interfaces:**
- Consumes: `StandardRef.doc_id`, `.page_number`, `.chunk_text` (from Task 4)
- Consumes: `doc_repo.find_doc_by_id()` (from Task 6)
- Produces: `IssueResponse` 含 `cited_excerpt`, `document_position`, `standard_doc_id`, `standard_page_number`, `standard_chunk_text`, `standard_file_type`

- [ ] **Step 1: 修改 IssueResponse 类**

```python
class IssueResponse(BaseModel):
    id: int
    type: str
    clause_number: str | None
    description: str
    severity: str
    standard_name: str | None
    standard_clause: str | None
    suggestion: str | None
    # 补齐 ——
    cited_excerpt: str | None = None
    document_position: str | None = None
    # 新增 ——
    standard_doc_id: str | None = None
    standard_page_number: int | None = None
    standard_chunk_text: str | None = None
    standard_file_type: str | None = None
```

- [ ] **Step 2: 修改 IssueResponse 构建逻辑**

在 `get_audit_result()` 函数中（约 line 137-149）：

```python
issues = []
for issue in result.issues:
    std_ref = issue.standard_reference
    # 根据 doc_id 查询 file_type
    file_type = None
    if std_ref and std_ref.doc_id:
        doc = doc_repo.find_doc_by_id(std_ref.doc_id)
        if doc:
            file_type = doc.file_type

    issues.append(IssueResponse(
        id=issue.id,
        type=issue.type,
        clause_number=issue.location.clause_number,
        description=issue.description,
        severity=issue.severity,
        standard_name=std_ref.standard_name if std_ref else None,
        standard_clause=std_ref.clause if std_ref else None,
        suggestion=issue.suggestion,
        # 补齐
        cited_excerpt=issue.cited_excerpt or None,
        document_position=issue.document_position or None,
        # 新增
        standard_doc_id=std_ref.doc_id if std_ref else None,
        standard_page_number=std_ref.page_number if std_ref else None,
        standard_chunk_text=std_ref.chunk_text if std_ref else None,
        standard_file_type=file_type,
    ))
```

- [ ] **Step 3: 验证**

```bash
# 启动后端测试 API 响应
curl http://localhost:8000/api/v1/audit-tasks/<task_id>/result | python -m json.tool | head -40
```

- [ ] **Step 4: Commit**

```bash
git add api/routers/audit_tasks.py
git commit -m "feat: extend IssueResponse with cited_excerpt, traceability and file_type fields"
```

---

### Task 8: CLI reindex 命令新增 --doc-id 参数

**Files:**
- Modify: `cli/main.py:114-122` (index rebuild 命令)

**Interfaces:**
- Consumes: `services.vector_search.rebuild_kb_index(kb_id)` (existing)
- Consumes: `services.doc_service.import_document()` — re-import single doc for page_texts

- [ ] **Step 1: 修改 index rebuild 命令**

```python
@index_app.command("rebuild")
def index_rebuild(
    kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID"),
    doc_id: str = typer.Option(None, "--doc-id", help="仅重建指定文档（需重新提取文本以获取页码信息）"),
):
    """重建知识库向量索引。--doc-id 指定时仅重建单个文档。"""
    if doc_id:
        import storage.doc_repo as doc_repo
        doc = doc_repo.get_doc(kb_id, doc_id)
        if not doc:
            typer.echo(f"文档不存在: {doc_id}")
            raise typer.Exit(1)

        if not doc.file_path or not Path(doc.file_path).exists():
            typer.echo(f"文档文件不存在: {doc.file_path}")
            raise typer.Exit(1)

        typer.echo(f"重新导入文档 {doc.original_name} 以获取页码信息...")
        with open(doc.file_path, "rb") as f:
            content = f.read()

        # 先删除旧索引
        from services.vector_search import remove_document_index
        remove_document_index(kb_id, doc_id)

        # 重新导入（会重新提取逐页文本并索引）
        import services.doc_service as doc_svc
        doc_svc.import_document(kb_id, doc.original_name, content, async_index=False)
        typer.echo(f"文档 {doc_id} 重建完成")
    else:
        typer.echo(f"开始重建知识库 {kb_id} 的向量索引...")
        from services.vector_search import rebuild_kb_index as rebuild_vec
        rebuild_vec(kb_id)
        typer.echo("向量索引重建完成")
```

- [ ] **Step 2: 验证**

```bash
uv run python -m cli index rebuild --kb-id <kb_id> --doc-id <doc_id>
```

- [ ] **Step 3: Commit**

```bash
git add cli/main.py
git commit -m "feat: add --doc-id to index rebuild for single-doc re-import with page info"
```

---

### Task 9: 前端类型 + PDF 查看器页面 + 路由 + 依赖

**Files:**
- Modify: `frontend/src/api/types.ts:70-81` (AuditIssue, AuditEventIssue)
- Create: `frontend/src/pages/PdfViewer.tsx`
- Modify: `frontend/src/App.tsx:1-32` (路由)
- Modify: `frontend/package.json` (新增 pdfjs-dist)

**Interfaces:**
- Consumes: `GET /api/v1/kb-documents/{doc_id}` — 获取 file_type, page_count 等
- Consumes: `GET /api/v1/kb-documents/{doc_id}/file` — 获取 PDF 文件流
- Consumes: `GET /api/v1/kb-documents/{doc_id}/page/{n}` — 文本降级

- [ ] **Step 1: 更新前端类型**

在 `frontend/src/api/types.ts` 中：

```typescript
export interface AuditIssue {
  id: number
  type: 'compliance' | 'completeness' | 'consistency' | 'insufficient_evidence' | 'out_of_scope'
  clause_number?: string
  description: string
  severity: 'high' | 'medium' | 'low'
  standard_name?: string
  standard_clause?: string
  suggestion?: string
  cited_excerpt?: string
  document_position?: string
  // 新增
  standard_doc_id?: string
  standard_page_number?: number
  standard_chunk_text?: string
  standard_file_type?: string
}

export interface AuditEventIssue {
  id: number
  type: string
  severity: string
  description: string
  standard_name?: string
  standard_clause?: string
  // 新增
  standard_doc_id?: string
  standard_page_number?: number
  standard_chunk_text?: string
}
```

- [ ] **Step 2: 安装 pdfjs-dist**

```bash
cd frontend && npm install pdfjs-dist
```

- [ ] **Step 3: 创建 PdfViewer 页面**

```tsx
// frontend/src/pages/PdfViewer.tsx
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import * as pdfjsLib from 'pdfjs-dist'

// 设置 worker
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url
).toString()

interface DocMeta {
  id: string
  name: string
  file_type: string
  page_count: number | null
}

export function PdfViewer() {
  const pathname = window.location.pathname
  const docId = pathname.split('/').pop() || ''
  const [searchParams] = useSearchParams()
  const targetPage = parseInt(searchParams.get('page') || '1', 10)
  const highlight = searchParams.get('highlight') || ''

  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [currentPage, setCurrentPage] = useState(targetPage)
  const [totalPages, setTotalPages] = useState(0)

  // 高亮状态
  const [highlightedText, setHighlightedText] = useState('')

  useEffect(() => {
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    // 获取文档元数据
    fetch(`${apiBase}/api/v1/kb-documents/${docId}`)
      .then(r => { if (!r.ok) throw new Error('文档不存在'); return r.json() })
      .then(m => setMeta(m))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [docId])

  useEffect(() => {
    if (!meta || meta.file_type !== 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const url = `${apiBase}/api/v1/kb-documents/${docId}/file`
    pdfjsLib.getDocument({ url, cMapUrl: 'https://unpkg.com/pdfjs-dist@4.0.379/cmaps/', cMapPacked: true })
      .promise.then(doc => {
        setPdfDoc(doc)
        setTotalPages(doc.numPages)
      })
      .catch(e => setError(`PDF 加载失败: ${e.message}`))
  }, [meta, docId])

  useEffect(() => {
    if (!pdfDoc || !canvasRef.current) return
    const pageNum = Math.min(Math.max(currentPage, 1), totalPages)
    pdfDoc.getPage(pageNum).then(page => {
      const canvas = canvasRef.current!
      const viewport = page.getViewport({ scale: 1.5 })
      canvas.height = viewport.height
      canvas.width = viewport.width
      const ctx = canvas.getContext('2d')!
      page.render({ canvasContext: ctx, viewport }).promise.then(() => {
        // 高亮文本搜索
        if (highlight && canvas.width > 0) {
          page.getTextContent().then(textContent => {
            const searchTerms = highlight.split(/\s+/).filter(t => t.length > 1)
            if (searchTerms.length === 0) return
            const scale = 1.5
            for (const item of textContent.items) {
              const textItem = item as { str: string; transform: number[] }
              const str = textItem.str || ''
              for (const term of searchTerms) {
                if (str.includes(term)) {
                  const tx = textItem.transform
                  const x = tx[4] * scale
                  const y = canvas.height - tx[5] * scale
                  const w = (str.length * (tx[0] || 8)) * scale * 0.6
                  const h = 14
                  ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
                  ctx.fillRect(x - 1, y - h, w + 2, h + 4)
                }
              }
            }
          })
        }
      })
    })
  }, [pdfDoc, currentPage, highlight, totalPages])

  // 文本降级模式（DOCX/MD）
  const [textContent, setTextContent] = useState('')
  const [textTotalPages, setTextTotalPages] = useState(0)

  useEffect(() => {
    if (!meta || meta.file_type === 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const page = Math.max(targetPage - 1, 0)
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/page/${page}`)
      .then(r => r.json())
      .then(d => { setTextContent(d.text); setTextTotalPages(d.total_pages) })
      .catch(e => setError(e.message))
  }, [meta, docId, targetPage])

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (error) return <div className="text-center py-20 text-red-500">{error}</div>
  if (!meta) return <div className="text-center py-20 text-slate-500">文档不存在</div>

  return (
    <div className="min-h-screen bg-slate-100">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">{meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {pdfDoc && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage <= 1} onClick={() => setCurrentPage(p => p - 1)}>←</button>
              <span className="text-slate-600 tabular-nums">{currentPage} / {totalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage >= totalPages} onClick={() => setCurrentPage(p => p + 1)}>→</button>
              <input type="number" className="w-14 px-2 py-1 border rounded text-center text-xs"
                min={1} max={totalPages} value={currentPage}
                onChange={e => { const v = parseInt(e.target.value); if (v >= 1 && v <= totalPages) setCurrentPage(v) }} />
            </>
          )}
          {highlight && <span className="text-xs text-amber-600 ml-2">🔍 高亮: {highlight.slice(0, 50)}</span>}
        </div>
      </div>

      {/* Content */}
      <div ref={containerRef} className="flex justify-center py-6">
        {meta.file_type === 'pdf' ? (
          <div className="bg-white shadow-lg rounded">
            <canvas ref={canvasRef} className="max-w-full" />
          </div>
        ) : (
          <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
            <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {textContent || '（该页无文本内容）'}
            </pre>
            {textTotalPages > 0 && (
              <p className="text-xs text-slate-400 mt-4">第 {targetPage} / {textTotalPages} 页</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 注册路由**

在 `frontend/src/App.tsx` 中：

```tsx
import { PdfViewer } from './pages/PdfViewer'

// 在 <Routes> 中增加:
<Route path="/pdf-viewer/:docId" element={<PdfViewer />} />
```

- [ ] **Step 5: 构建验证**

```bash
cd frontend && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/pages/PdfViewer.tsx frontend/src/App.tsx frontend/package.json frontend/package-lock.json
git commit -m "feat: add PdfViewer page with pdfjs-dist for PDF preview and text highlight"
```

---

### Task 10: 前端审核结果页 + 流式面板链接渲染

**Files:**
- Modify: `frontend/src/pages/AuditResult.tsx:202-208` (标准依据区域)
- Modify: `frontend/src/components/AuditStream.tsx:41-58` (IssueCard)

**Interfaces:**
- Consumes: `AuditIssue.standard_doc_id`, `.standard_page_number`, `.standard_chunk_text`, `.standard_file_type` (from Task 9 types)
- Consumes: `AuditEventIssue` new fields

- [ ] **Step 1: 修改 AuditResult.tsx — 标准依据链接**

在 `frontend/src/pages/AuditResult.tsx` 中，将标准依据展示改为可点击链接（约 line 202-208）：

```tsx
{/* 标准依据 + 建议 — 替换原有蓝色框中的依据部分 */}
{(issue.standard_name || issue.suggestion) && (
  <div className="mt-2 text-xs text-slate-500 bg-blue-50/40 rounded-md p-2.5 border border-blue-100/60">
    {issue.standard_name && (
      <p>
        <span className="font-medium text-slate-600">依据：</span>
        {issue.standard_doc_id ? (
          <a
            href={`/pdf-viewer/${issue.standard_doc_id}?page=${issue.standard_page_number ?? ''}&clause=${encodeURIComponent(issue.standard_clause || '')}&highlight=${encodeURIComponent(issue.standard_chunk_text || '')}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline cursor-pointer"
          >
            📄 {issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}
          </a>
        ) : (
          <span>{issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}</span>
        )}
      </p>
    )}
    {issue.suggestion && <p className="mt-1"><span className="font-medium text-slate-600">建议：</span>{issue.suggestion}</p>}
  </div>
)}
```

- [ ] **Step 2: 修改 AuditStream.tsx — IssueCard 链接**

在 `frontend/src/components/AuditStream.tsx` 的 `IssueCard` 组件中（约 line 41-58），修改标准依据区域：

```tsx
function IssueCard({ issue }: { issue: AuditEventIssue }) {
  const pdfUrl = issue.standard_doc_id
    ? `/pdf-viewer/${issue.standard_doc_id}?page=${issue.standard_page_number ?? ''}&clause=${encodeURIComponent(issue.standard_clause || '')}&highlight=${encodeURIComponent(issue.standard_chunk_text || '')}`
    : null

  return (
    <div className={`mt-1 px-3 py-2 rounded-md border text-sm ${severityColors[issue.severity] || severityColors.medium}`}>
      <div className="flex items-center gap-2">
        <AlertTriangle className="w-3.5 h-3.5" />
        <span className="font-medium">
          #{issue.id} [{issue.severity === 'high' ? '高' : issue.severity === 'medium' ? '中' : '低'}风险]
        </span>
        <Badge value={issue.type} />
      </div>
      <p className="mt-1 leading-relaxed">{issue.description}</p>
      {(issue.standard_name || issue.standard_clause) && (
        <p className="mt-0.5 text-xs opacity-70">
          依据:{' '}
          {pdfUrl ? (
            <a href={pdfUrl} target="_blank" rel="noopener noreferrer"
              className="text-blue-500 hover:underline cursor-pointer">
              📄 {issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}
            </a>
          ) : (
            <span>{issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}</span>
          )}
        </p>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 构建验证**

```bash
cd frontend && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AuditResult.tsx frontend/src/components/AuditStream.tsx
git commit -m "feat: add clickable PDF jump links in audit result and stream panels"
```

---

## 验证计划

完成所有任务后，端到端验证：

```bash
# 1. 启动后端
pkill -f "uvicorn api.main" 2>/dev/null; sleep 1
nohup uv run uvicorn api.main:app --port 8000 > /tmp/backend.log 2>&1 &

# 2. 导入一个 PDF 到知识库
uv run python -m cli kb create --name "test-kb" --category national
# 记录 kb_id
uv run python -m cli doc import --kb-id <kb_id> --file sample_docs/sample.pdf

# 3. 验证索引 chunk 包含 page_number
uv run python -c "
from core.index_manager import search
results = search(['<kb_id>'], 'test', top_k=3)
for r in results:
    print(f'source={r[\"doc_source\"]}, page_number={r.get(\"page_number\")}, doc_id={r.get(\"doc_id\")}')
"

# 4. 上传待审核文档并运行审核
uv run python -m cli audit upload --file sample_docs/sample.pdf
# 记录 doc_id
uv run python -m cli audit-task create --doc-id <doc_id> --kb-ids <kb_id> --sync

# 5. 检查审核结果 API 是否包含溯源字段
curl -s http://localhost:8000/api/v1/audit-tasks/<task_id>/result | python -m json.tool | grep -E "standard_doc_id|standard_page_number|standard_chunk_text"

# 6. 测试 PDF 文件端点（Range 请求）
curl -I -H "Range: bytes=0-1023" http://localhost:8000/api/v1/kb-documents/<doc_id>/file

# 7. 前端构建
cd frontend && npm run build

# 8. 启动前端开发服务器，在浏览器中验证：
#    - 审核结果页标准依据显示为可点击链接
#    - 点击后新标签页打开 PDF 查看器
#    - PDF 跳转到正确页面并高亮条款文字
```
