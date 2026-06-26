# 审核问题标准依据自动链接 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 审核完成后自动将 issue 中引用的标准与 KB 中对应文档关联，填入 `standard_doc_id` 等字段。

**Architecture:** 在 `_run_native_tool_calling` 和 `_run_structured_llm_loop` 的 `_build_result()` 前插入 `_link_standards_to_kb()` 后处理步骤。该函数筛选 `standard_doc_id` 为空的 issue，用一次 LLM 调用（JSON Output 模式）批量提取标准编号/名称，再跨 KB 搜索匹配文档（文本搜索优先、向量搜索兜底、结果缓存），回填 `standard_doc_id`、`standard_page_number`、`standard_chunk_text`。

**Tech Stack:** Python, DeepSeek API (JSON Output mode), rga (ripgrep-all), LlamaIndex FAISS vector search

## Global Constraints

- 不改动前端代码
- 不修改数据模型（Pydantic schema）
- 任何步骤失败不阻塞审核结果返回
- 遵循现有代码风格（中文注释、`_` 前缀内部函数、`_logger` 日志）

---

## File Structure

| 文件 | 职责 |
|------|------|
| `services/vector_search.py` | 新增 `search_doc_by_text()` — 用 rga 搜索 KB 文档原文并返回结构化结果 |
| `services/agentic_audit.py` | 新增 `_extract_standard_info()` — LLM 批量提取；`_search_and_link_standards()` — 搜索+回填+缓存；`_link_standards_to_kb()` — 入口；修改两处 `_build_result()` 前插入调用 |

---

### Task 1: `search_doc_by_text()` 文本搜索接口

**Files:**
- Modify: `services/vector_search.py`

**Interfaces:**
- Produces: `search_doc_by_text(keyword: str, kb_ids: list[str]) -> list[dict]`
  - Returns `[{doc_id, page_number, content}]`，page_number 可能为 None（文本文件无页码）
  - 内部使用 `_run_rga` + `_get_kb_search_paths`（已存在于同文件）

- [ ] **Step 1: Add `search_doc_by_text()` function**

在 `services/vector_search.py` 文件末尾添加：

```python
def search_doc_by_text(keyword: str, kb_ids: list[str]) -> list[dict]:
    """用 rga 精确搜索 KB 文档原文，返回结构化结果。
    
    适用于搜索标准编号（如 GB/T 20145-2006）等在文档正文中
    精确出现的字符串。不依赖文件名，搜的是文档内容。
    
    Returns:
        [{doc_id, page_number, content}]
        page_number 为 None 表示无法确定页码（非 PDF 或解析失败）。
    """
    if not keyword or not kb_ids:
        return []
    
    paths = _get_kb_search_paths(kb_ids)
    if not paths:
        return []
    
    raw = _run_rga(keyword, paths)
    if not raw:
        return []
    
    # 构建 file_path -> (kb_id, doc_id) 映射
    import storage.doc_repo as doc_repo
    file_to_doc: dict[str, tuple[str, str]] = {}  # resolved_path -> (kb_id, doc_id)
    for kb_id in kb_ids:
        for doc in doc_repo.list_docs(kb_id):
            fp = str(Path(doc.file_path).resolve())
            file_to_doc[fp] = (kb_id, doc.id)
    
    # 解析 rga 输出，提取文件路径和匹配行
    # rga 输出格式: /path/to/file:line_num:text
    hits: list[dict] = []
    seen_doc_ids: set[str] = set()
    
    for line in raw.split("\n"):
        # rga 格式: /absolute/path/to/file:123:text content
        # 也支持 -- 分隔符: /path/to/file-123-text content (部分 rga 版本)
        match = re.match(r"^(.+?):(\d+):(.*)", line)
        if not match:
            # 可能是上下文行（-C 参数产生的）
            match_ctx = re.match(r"^(.+?)-(\d+)-(.*)", line)
            if not match_ctx:
                continue
            file_path_raw, line_num_str, text = match_ctx.groups()
        else:
            file_path_raw, line_num_str, text = match.groups()
        
        file_path = str(Path(file_path_raw).resolve())
        if file_path not in file_to_doc:
            continue
        
        kb_id, doc_id = file_to_doc[file_path]
        if doc_id in seen_doc_ids:
            continue
        
        seen_doc_ids.add(doc_id)
        
        # 对于 PDF，page_number 无法从 rga 行号直接获取
        # 返回 doc_id，page_number 由后续向量搜索补充
        hits.append({
            "doc_id": doc_id,
            "kb_id": kb_id,
            "page_number": None,
            "content": text.strip()[:500],
        })
        
        if len(hits) >= 5:
            break
    
    return hits
```

- [ ] **Step 2: Verify the function exists**

```bash
uv run python -c "from services.vector_search import search_doc_by_text; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/vector_search.py
git commit -m "feat: add search_doc_by_text() for KB text search with doc_id mapping"
```

---

### Task 2: `_extract_standard_info()` LLM 提取

**Files:**
- Modify: `services/agentic_audit.py`

**Interfaces:**
- Consumes: `AuditIssue` (from `models.audit_task`)
- Produces: `_extract_standard_info(issues: list[AuditIssue]) -> dict[int, dict]`
  - Returns `{issue_id: {standard_numbers: [str], standard_names: [str]}}`
  - 空 dict 表示提取失败或无标准

- [ ] **Step 1: Add `_extract_standard_info()` function**

在 `services/agentic_audit.py` 的 `_build_result` 函数之前（约第 620 行）添加：

```python
def _extract_standard_info(issues: list[AuditIssue]) -> dict[int, dict]:
    """用 LLM 批量从 issue 文本中提取标准编号和名称。
    
    Args:
        issues: standard_doc_id 为空的 issue 列表
    
    Returns:
        {issue.id: {standard_numbers: [...], standard_names: [...]}}
        提取不到任何标准的 issue 返回空数组
    """
    if not issues:
        return {}
    
    # 构建提取输入
    input_items = []
    for iss in issues:
        item = {"id": iss.id}
        if iss.standard_reference:
            sn = (iss.standard_reference.standard_name or "").strip()
            if sn:
                item["standard_name"] = sn
        item["description"] = iss.description or ""
        item["cited_excerpt"] = iss.cited_excerpt or ""
        item["suggestion"] = iss.suggestion or ""
        input_items.append(item)
    
    system_prompt = """你是一个标准文献信息提取器。从审核问题的描述文本中提取被引用的标准编号和标准名称，输出 JSON 格式。

规则：
1. standard_numbers: 标准编号列表，如 "GB/T 20145-2006"、"GB 50016"、"CJJ 101-2016"。
   不含纯数字编号（如"12345"不算）。从 description、cited_excerpt、suggestion 字段中提取。
2. standard_names: 标准中文名称列表，不含书名号《》，如 "灯和灯系统的光生物安全性"。
3. standard_name 字段如果已有值直接复用，无需重复提取。
4. 如果问题没有涉及任何可识别的标准，返回空数组。

输入格式: {"issues": [{"id": 1, "standard_name": "...", "description": "...", "cited_excerpt": "...", "suggestion": "..."}]}

输出格式: {"results": [{"id": 1, "standard_numbers": ["GB/T 20145-2006"], "standard_names": ["灯和灯系统的光生物安全性"]}]}"""

    user_prompt = json.dumps({"issues": input_items}, ensure_ascii=False)
    
    try:
        import httpx
        from openai import OpenAI
        
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            _logger.warning("_extract_standard_info: DEEPSEEK_API_KEY not set, skipping")
            return {}
        
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        # 使用轻量模型做提取（不需要深度推理）
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        
        http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60))
        client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=4096,
        )
        
        content = response.choices[0].message.content
        if not content:
            _logger.warning("_extract_standard_info: empty response from LLM")
            return {}
        
        data = json.loads(content)
        results_list = data.get("results", [])
        
        output: dict[int, dict] = {}
        for item in results_list:
            iss_id = item.get("id")
            if iss_id is None:
                continue
            nums = item.get("standard_numbers", []) or []
            names = item.get("standard_names", []) or []
            if nums or names:
                output[iss_id] = {
                    "standard_numbers": nums,
                    "standard_names": names,
                }
        
        _logger.info(
            "_extract_standard_info: extracted standards for %d/%d issues",
            len(output), len(issues),
        )
        return output
        
    except Exception as e:
        _logger.warning("_extract_standard_info failed: %s", e)
        return {}
```

- [ ] **Step 2: Verify import and syntax**

```bash
uv run python -c "from services.agentic_audit import _extract_standard_info; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/agentic_audit.py
git commit -m "feat: add _extract_standard_info() for LLM-based standard extraction"
```

---

### Task 3: `_search_and_link_standards()` 搜索与回填 + `_link_standards_to_kb()` 入口

**Files:**
- Modify: `services/agentic_audit.py`

**Interfaces:**
- Consumes: `AuditIssue`, `_extract_standard_info()` 返回值
- Produces: `_link_standards_to_kb(issues: list[AuditIssue], kb_ids: list[str]) -> None` — 原地修改 issues

- [ ] **Step 1: Add `_search_and_link_standards()` function**

在 `_extract_standard_info` 之后添加：

```python
def _search_and_link_standards(
    issues: list[AuditIssue],
    kb_ids: list[str],
    extracted: dict[int, dict],
) -> None:
    """搜索知识库并回填 standard_doc_id 等字段。
    
    搜索策略（按优先级）：
    1. 精确文本搜索（rga）— 标准编号
    2. 向量语义搜索 — 标准编号 + 标准名称
    3. 结果精确验证 — 命中内容的 content 必须包含标准编号
    
    结果缓存：同一标准编号只搜一次。
    
    Args:
        issues: 待处理的 issue 列表（原地修改）
        kb_ids: 审核任务关联的知识库 ID 列表
        extracted: _extract_standard_info() 的返回值
    """
    if not issues or not kb_ids:
        return
    
    from services.vector_search import search_doc_by_text, vec_search
    
    # 搜索结果缓存：standard_number -> {doc_id, page_number, chunk_text} | None
    _search_cache: dict[str, dict | None] = {}
    
    # 按 issue id 索引
    issue_by_id = {iss.id: iss for iss in issues}
    
    for iss_id, info in extracted.items():
        issue = issue_by_id.get(iss_id)
        if not issue or not issue.standard_reference:
            continue
        
        standard_numbers = info.get("standard_numbers", []) or []
        standard_names = info.get("standard_names", []) or []
        
        best_hit = None
        
        # ── 策略1: 精确文本搜索 ──
        for std_num in standard_numbers:
            if std_num in _search_cache:
                best_hit = _search_cache[std_num]
                break
            
            text_hits = search_doc_by_text(std_num, kb_ids)
            if text_hits:
                # 文本搜索命中了文档，但缺少 page_number
                # 用向量搜索补充 page_number 和 chunk_text
                query = f"{std_num} {standard_names[0]}" if standard_names else std_num
                vec_hits = vec_search(kb_ids, query, top_k=3)
                
                # 精确验证：vec hits 的 content 必须包含标准编号
                matched_doc_ids = {h["doc_id"] for h in text_hits}
                for vh in vec_hits:
                    if vh["doc_id"] in matched_doc_ids:
                        content = vh.get("content", "")
                        if any(sn in content for sn in standard_numbers):
                            best_hit = {
                                "doc_id": vh["doc_id"],
                                "page_number": vh.get("page_number"),
                                "chunk_text": content[:500],
                            }
                            break
                
                _search_cache[std_num] = best_hit
                break
        
        # ── 策略2: 向量语义搜索（文本搜索无结果时） ──
        if not best_hit and (standard_numbers or standard_names):
            query_parts = standard_numbers + standard_names
            query = " ".join(query_parts[:3])  # 最多3个词
            vec_hits = vec_search(kb_ids, query, top_k=5)
            
            for vh in vec_hits:
                content = vh.get("content", "")
                # 精确验证
                verified = False
                if standard_numbers:
                    verified = any(sn in content for sn in standard_numbers)
                else:
                    verified = any(nm in content for nm in standard_names)
                
                if verified:
                    best_hit = {
                        "doc_id": vh["doc_id"],
                        "page_number": vh.get("page_number"),
                        "chunk_text": content[:500],
                    }
                    if standard_numbers:
                        _search_cache[standard_numbers[0]] = best_hit
                    break
        
        # ── 回填 ──
        if best_hit:
            sr = issue.standard_reference
            sr.doc_id = best_hit["doc_id"]
            sr.page_number = best_hit.get("page_number")
            sr.chunk_text = best_hit.get("chunk_text")
            # 如果 standard_name 为空，用提取到的首条编号补上
            if not sr.standard_name and standard_numbers:
                sr.standard_name = standard_numbers[0]
                sr.standard_id = standard_numbers[0]
            _logger.info(
                "_search_and_link_standards: linked issue #%d to doc %s",
                issue.id, best_hit["doc_id"],
            )
```

- [ ] **Step 2: Add `_link_standards_to_kb()` entry point**

同上位置继续添加：

```python
def _link_standards_to_kb(
    issues: list[AuditIssue],
    kb_ids: list[str],
) -> None:
    """审核后处理：将 issue 中引用的标准关联到 KB 文档。
    
    筛选 standard_doc_id 为空的 issue → LLM 提取标准信息 →
    搜索 KB → 回填 doc_id/page_number/chunk_text。
    
    任何步骤失败都不影响审核结果。
    """
    if not issues or not kb_ids:
        return
    
    # 筛选：standard_doc_id 为空的 issue
    pending = []
    for iss in issues:
        if iss.standard_reference and not iss.standard_reference.doc_id:
            pending.append(iss)
    
    if not pending:
        return
    
    _logger.info("_link_standards_to_kb: %d issues need standard linking", len(pending))
    
    try:
        extracted = _extract_standard_info(pending)
    except Exception as e:
        _logger.warning("_link_standards_to_kb: extraction failed: %s", e)
        return
    
    if not extracted:
        return
    
    try:
        _search_and_link_standards(pending, kb_ids, extracted)
    except Exception as e:
        _logger.warning("_link_standards_to_kb: search failed: %s", e)
```

- [ ] **Step 3: Verify imports and syntax**

```bash
uv run python -c "from services.agentic_audit import _link_standards_to_kb, _search_and_link_standards; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add services/agentic_audit.py
git commit -m "feat: add _search_and_link_standards() and _link_standards_to_kb() entry point"
```

---

### Task 4: 接入两个审核循环

**Files:**
- Modify: `services/agentic_audit.py`

**Interfaces:**
- Consumes: `_link_standards_to_kb()` (from Task 3)
- Produces: (none — side effect on issues list before _build_result)

- [ ] **Step 1: 在 `_run_native_tool_calling` 中接入**

找到 `services/agentic_audit.py` 中 `_run_native_tool_calling` 函数末尾的 `_save_trace()` 和 `return _build_result(...)` 之间（约第 1246–1248 行），在 `_save_trace(...)` 之后、`return _build_result(...)` 之前插入：

```python
    # 后处理：将 issue 中引用的标准关联到知识库文档
    _link_standards_to_kb(issues, kb_ids)
```

修改后的代码为：

```python
    _save_trace(
        task_id, doc_id, doc_name,
        issues_count=len(issues),
        total_iterations=iteration + 1,
        messages=messages,
        provider="deepseek",
        model=model,
        finished=finished,
    )

    # 后处理：将 issue 中引用的标准关联到知识库文档
    _link_standards_to_kb(issues, kb_ids)

    return _build_result(task_id, doc_id, doc_name, issues, raw_analysis)
```

- [ ] **Step 2: 在 `_run_structured_llm_loop` 中接入**

找到 `_run_structured_llm_loop` 函数末尾的 `_save_trace()` 和 `return _build_result(...)` 之间（约第 1431–1433 行），同样插入：

```python
    # 后处理：将 issue 中引用的标准关联到知识库文档
    _link_standards_to_kb(issues, kb_ids)
```

修改后的代码为：

```python
    _save_trace(
        task_id, doc_id, doc_name,
        issues_count=len(issues),
        total_iterations=turn + 1,
        messages=serializable_messages,
        provider=os.environ.get("LLM_PROVIDER", "unknown"),
        finished=finished,
    )

    # 后处理：将 issue 中引用的标准关联到知识库文档
    _link_standards_to_kb(issues, kb_ids)

    return _build_result(task_id, doc_id, doc_name, issues, raw_analysis)
```

- [ ] **Step 3: Verify syntax**

```bash
uv run python -c "from services.agentic_audit import run_agentic_audit; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add services/agentic_audit.py
git commit -m "feat: integrate standard-KB auto-linking into both audit loops"
```

---

### Task 5: 端到端验证

**Files:**
- 无需新建或修改文件

- [ ] **Step 1: 确认所有模块导入正常**

```bash
uv run python -c "
from services.vector_search import search_doc_by_text
from services.agentic_audit import (
    _extract_standard_info,
    _search_and_link_standards,
    _link_standards_to_kb,
    run_agentic_audit,
)
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 2: 单元测试 — `search_doc_by_text` 不存在的标准**

```bash
uv run python -c "
from services.vector_search import search_doc_by_text
result = search_doc_by_text('XYZ-99999-NONEXISTENT', ['01KW0XRE1FRJF2WFJ4QWVVSW4K'])
assert result == [], f'Expected empty, got {result}'
print('PASS: nonexistent standard returns empty')
"
```

Expected: `PASS: nonexistent standard returns empty`

- [ ] **Step 3: 单元测试 — `search_doc_by_text` 存在的标准**

```bash
uv run python -c "
from services.vector_search import search_doc_by_text
result = search_doc_by_text('GB50034', ['01KW0XRE1FRJF2WFJ4QWVVSW4K'])
# 国标库中有 50-GB50034-2013.pdf
assert len(result) > 0, f'Expected non-empty, got {result}'
print(f'PASS: found {len(result)} hits for GB50034')
for r in result:
    print(f'  doc_id={r[\"doc_id\"]}, content={r[\"content\"][:80]}')
"
```

Expected: `PASS: found N hits for GB50034`

- [ ] **Step 4: 单元测试 — `_extract_standard_info` 提取**

```bash
# 先确保 DEEPSEEK_API_KEY 已设置
uv run python -c "
import os
assert os.environ.get('DEEPSEEK_API_KEY'), 'DEEPSEEK_API_KEY not set'
from models.audit_task import AuditIssue, StandardRef, IssueLocation
from services.agentic_audit import _extract_standard_info

# 构造模拟 issue（模拟 Issue #5 的场景）
issue = AuditIssue(
    id=1,
    type='compliance',
    severity='medium',
    description='文档提到\"符合现行国家标准《灯和灯系统的光生物安全性》的有关规定\"，但未给出标准编号。该标准应为GB/T 20145-2006。',
    cited_excerpt='人员长期停留的场所应采用符合现行国家标准《灯和灯系统的光生物安全性》的有关规定',
    document_position='第五章 技术规格及需求',
    standard_reference=StandardRef(
        standard_name='',
        standard_id='',
    ),
    location=IssueLocation(original_text=''),
)

result = _extract_standard_info([issue])
print(f'Extracted: {result}')
assert 1 in result, f'Issue #1 should have extraction'
info = result[1]
assert 'GB/T 20145-2006' in info.get('standard_numbers', []), \
    f'Expected GB/T 20145-2006 in standard_numbers, got {info}'
print('PASS: extraction works correctly')
"
```

Expected: `PASS: extraction works correctly`

- [ ] **Step 5: 单元测试 — `_link_standards_to_kb` 回填逻辑（不调用 LLM 时跳过）**

```bash
uv run python -c "
from models.audit_task import AuditIssue, StandardRef, IssueLocation
from services.agentic_audit import _link_standards_to_kb

# standard_doc_id 已有值的 issue 应被跳过
issues = [
    AuditIssue(
        id=1, type='compliance', severity='medium',
        description='test',
        standard_reference=StandardRef(
            standard_name='GB 50034', standard_id='GB 50034',
            doc_id='existing_doc_id',  # 已有值
        ),
        location=IssueLocation(original_text=''),
    ),
]
_link_standards_to_kb(issues, ['01KW0XRE1FRJF2WFJ4QWVVSW4K'])
assert issues[0].standard_reference.doc_id == 'existing_doc_id', \
    'doc_id should not change when already set'
print('PASS: issues with existing doc_id are skipped')

# 空的 issues 列表不报错
_link_standards_to_kb([], [])
print('PASS: empty issues handled gracefully')

# 空的 kb_ids 不报错
issues2 = [
    AuditIssue(
        id=1, type='compliance', severity='medium',
        description='test',
        standard_reference=StandardRef(),
        location=IssueLocation(original_text=''),
    ),
]
_link_standards_to_kb(issues2, [])
print('PASS: empty kb_ids handled gracefully')
"
```

Expected: 全部 `PASS`

- [ ] **Step 6: 完整流程测试 — 审核后 issue 是否被回填**

启动后端，触发一次审核，观察日志：

```bash
# 在另一个终端启动后端
uv run uvicorn api.main:app --port 8000

# 触发对已有标准文档的审核
# 观察日志中是否出现:
# "_link_standards_to_kb: N issues need standard linking"
# "_extract_standard_info: extracted standards for N/N issues"
# "_search_and_link_standards: linked issue #N to doc XXX"
```

- [ ] **Step 7: Commit 测试结果**

```bash
git add -A
git commit -m "test: verify standard-KB auto-linking end-to-end"
```
