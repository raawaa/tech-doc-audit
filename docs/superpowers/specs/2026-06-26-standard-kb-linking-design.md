# 审核问题标准依据自动链接

## 背景

当前 Agentic 审核中，LLM 调用 `flag_issue` 时经常不填 `standard_doc_id`（可选字段），有时甚至连 `standard_name` 也不填。但 issue 的自由文本（`description`、`cited_excerpt`、`suggestion`）中明确提到了相关标准编号和名称。

前端 `AuditResult.tsx` 和 `AuditStream.tsx` 已支持当 `standard_doc_id` 存在时渲染可点击的 PDF 跳转链接（`/pdf-viewer/{docId}?page=N&highlight=...`）。只需要把 `standard_doc_id` 填上，链接就能工作。

## 目标

审核完成后，自动将 issue 中引用的标准与知识库中的标准文档关联。如果知识库里有该标准，就填入 `standard_doc_id`、`standard_page_number`、`standard_chunk_text`，让用户能点击链接预览 PDF，确认标准的真实性。

知识库里没有的标准则保持原样不处理。

## 整体流程

在审核循环（`_run_native_tool_calling` / `_run_structured_llm_loop`）结束后、`_build_result()` 之前，插入后处理步骤：

```
ReAct loop 结束 → issues 列表
  ↓
筛选 standard_doc_id 为空的 issue
  ↓
LLM 批量提取标准编号 + 名称（JSON Output 模式）
  ↓
跨 KB 搜索标准文档（文本搜索优先，向量搜索兜底，结果缓存）
  ↓
回填 standard_doc_id / page_number / chunk_text
  ↓
_build_result() 照常构建
```

任何步骤失败都不影响审核结果 — 跳过该 issue，保持原样。

## 模块设计

### 1. LLM 提取：`_extract_standard_info(issues) -> dict`

**输入**：`standard_doc_id` 为空的 issue 列表，每个 issue 取其 `standard_name`、`description`、`cited_excerpt`、`suggestion` 四个字段。

**输出**：`{issue_id: {standard_numbers: [...], standard_names: [...]}}`

**实现要点**：
- 一次 LLM 调用批量处理所有待处理 issue
- 使用 DeepSeek JSON Output 模式（`response_format: {'type': 'json_object'}`），确保合法 JSON
- Temperature = 0，模型用 DeepSeek V3（轻量，提取任务无需深度推理）
- `standard_name` 已有值的直接复用，无需重复提取
- 提取不到任何标准的 issue 返回空数组，后续跳过

**Prompt 示例**：
```
你是一个标准文献信息提取器。从以下审核问题的描述中提取被引用的标准编号和标准名称。

输入格式：{"issues": [{"id": 1, "description": "...", ...}]}
返回格式：{"results": [{"id": 1, "standard_numbers": [...], "standard_names": [...]}]}

规则：
- standard_numbers: 标准编号，如 "GB/T 20145-2006"、"GB 50016"，不含纯数字编号
- standard_names: 标准中文名称，不含书名号《》
- 如果问题没有涉及可识别的标准，返回空数组
- standard_name 字段已有值的，直接复用，无需重复提取
```

**文件位置**：`services/agentic_audit.py`

### 2. KB 搜索：`_search_and_link_standards(issues, kb_ids, extracted_info)`

对每个 issue 的提取结果，搜索知识库并回填。

**搜索策略**（按优先级）：

1. **精确文本搜索** — 用 `standard_numbers[0]` 调 `_run_rga()` 搜索 KB 文档原文。标准文档正文必然包含自己的编号，命中率高。
2. **向量语义搜索** — 文本搜索无结果时，用 `标准编号 + 标准名称` 拼接为 query 调 `vec_search()`。
3. **精确验证** — 向量搜索结果需验证 `content` 中是否包含任一 `standard_numbers`，防止语义相关但实际不是同一标准的误命中。

**结果缓存**：同一标准编号被多个 issue 引用时，只搜一次。用 `dict[str, dict | None]` 缓存（key = 标准编号，value = 搜索结果或 None）。

**回填逻辑**：

取最佳命中的结果：
- `standard_doc_id` ← `hit["doc_id"]`
- `standard_page_number` ← `hit["page_number"]`
- `standard_chunk_text` ← `hit["content"]`
- `standard_name` / `standard_id` — 如果原本为空，用提取到的 `standard_numbers[0]` 补上

**跨 KB 搜索**：搜索审核任务关联的全部 KB。

**新增接口**：`services/vector_search.py` 暴露 `search_doc_by_text(keyword, kb_ids) -> list[dict]`，返回 `[{doc_id, page_number, content}]`。内部用 rga 搜索文件 + 文件路径 → doc_id 映射。

**文件位置**：`services/agentic_audit.py`（主逻辑）、`services/vector_search.py`（暴露文本搜索接口）

### 3. 入口函数：`_link_standards_to_kb(issues, kb_ids)`

串联步骤 1 和 2。在 `_run_native_tool_calling()` 和 `_run_structured_llm_loop()` 的 `_build_result()` 调用前插入。

```python
def _link_standards_to_kb(
    issues: list[AuditIssue],
    kb_ids: list[str],
) -> None:
    """后处理：将 issue 中引用的标准关联到 KB 文档。原地修改 issues。"""
    # 1. 筛选需要处理的 issue
    pending = [i for i in issues if i.standard_reference and not i.standard_reference.doc_id]
    if not pending:
        return
    
    # 2. LLM 提取标准信息
    try:
        extracted = _extract_standard_info(pending)
    except Exception:
        return  # 提取失败，跳过
    
    # 3. 搜索并回填
    _search_and_link_standards(pending, kb_ids, extracted)
```

### 4. 前端

**无需改动**。`AuditResult.tsx` 和 `AuditStream.tsx` 已有逻辑：

```tsx
{issue.standard_doc_id ? (
  <a href={`/pdf-viewer/${issue.standard_doc_id}?page=...&highlight=...`}>
    {issue.standard_name}
  </a>
) : (
  <span>{issue.standard_name}</span>
)}
```

后端填上 `standard_doc_id` 后，前端自动变可点击链接。

## 错误处理

| 场景 | 行为 |
|------|------|
| LLM 提取失败（API 错误、超时） | 跳过所有，issue 保持原样 |
| LLM 返回空结果 | 跳过，issue 保持原样 |
| 文本搜索无结果 | 降级到向量搜索 |
| 向量搜索无结果 | 跳过该 issue |
| rga 不可用 | 直接走向量搜索 |
| 文件路径 → doc_id 映射失败 | 跳过该命中 |
| 整个函数异常 | catch 后 return，不阻塞审核结果返回 |

## 改动清单

| 文件 | 改动 |
|------|------|
| `services/agentic_audit.py` | 新增 `_extract_standard_info()`、`_search_and_link_standards()`、`_link_standards_to_kb()`；在两个 audit loop 函数的 `_build_result()` 前插入 `_link_standards_to_kb()` 调用 |
| `services/vector_search.py` | 新增 `search_doc_by_text(keyword, kb_ids)` → 用 rga 搜索 KB 文档原文，返回 `[{doc_id, page_number, content}]` |

## 测试要点

1. `standard_doc_id` 已存在的 issue 不被处理（不浪费 LLM 调用）
2. LLM 正确提取标准编号 `GB/T 20145-2006` 和名称 `灯和灯系统的光生物安全性`
3. 标准存在于 KB 时，`standard_doc_id` 被正确填入，前端链接可点击
4. 标准不存在于 KB 时，issue 保持原样
5. 同一标准被多个 issue 引用时，只搜索一次（缓存命中）
6. LLM 调用失败不影响审核结果
7. 跨 KB 搜索正确（搜索所有关联 KB）
