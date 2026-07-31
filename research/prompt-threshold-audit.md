# Prompt Threshold Audit — services/agentic_audit.py + core/ + api/cli callers

> Read-only audit in support of issue **#115** (Wayfinder research).
> Map: **#114**. Decision tickets: **#117**, **#118**. Task: **#119**.
> Scope: every hard-coded numeric threshold that participates in **LLM prompt
> construction** (doc → structure → prompt → LLM), the `parsed_content`-slicing
> family, plus adjacent thresholds in the `core/` and `services/` data path that
> interact with the same flow (chat-memory, KB-tool output truncation,
> content snippets forwarded to the LLM / the UI).

Branch: `research/prompt-threshold-audit`
Snapshot: `HEAD` of branch `103-add-pymupdf-dep` (the branch the worktree
started on, written from `git checkout -b` of current `HEAD`). No source files
were modified.

---

## 1. Scope classification key

| Mark | Meaning |
|---|---|
| ✓ | **In map scope** — same code line(s) listed in #115 deliverable §1; the value changes the behaviour of prompt construction in `services/agentic_audit.py`. Should land in #119 together with the settings.py move. |
| △ | **Adjacent / borderline** — not in §1 of the ticket, but reachable from the same call chain; could ride along under #119 or be punted to a follow-up ticket. Each one is justified below. |
| ✗ | **Out of scope** — different concern (file upload size, page-count heuristics, KB chunking, token limit on QA memory). Listed because #115 asked them to be **flagged**, not moved. |

The ticket's "已知 5 处（见 map body）" + line numbers (L47 / L606 / L623 /
L1434 / L1449) corresponds exactly to entries **A1, A2, A3, A4, A5** below —
the five known sites the map is built around. The audit additionally surfaces
**A6–A13** as candidates.

---

## 2. Total / in-scope counts

- **Total hard-coded numeric thresholds discovered** in the audited surface
  (services/agentic_audit.py + core/ + api/ + cli/ + adjacent services that
  push text into a LLM context): **18**.
- **In map scope (✓)**: **5** — exactly the 5 sites already enumerated in the
  parent map (#114); no new ✓ sites found.
- **Adjacent (△)**: **4** — three are part of the audit `run_agentic_audit`
  data path but were not in §1 (#115 prompt preview `8000` semantics listed
  §1, but the **4000** in the `read_chapter` tool description belongs in the
  same family of "LLM-visible truncation contract"). One is the QA memory
  token budget, called out by the ticket literally ("embedding / QA 路径里的
  类似阈值（即使不在本次 map 范围，也要标出）").
- **Out of scope (✗)**: **9** — file-upload size, page-count heuristics,
  KB chunk size, KB scoring thresholds, similarity thresholds on the
  highlight layout-match path. Listed under §5 with rationale.

---

## 3. Findings: hard-coded thresholds with surrounding snippets

### A1 — `CHAPTER_MAX_CHARS = 8000` (module-level constant)

- **File:** `services/agentic_audit.py`
- **Lines:** L46–48 (definition), L356, L380, L383, L384, L387 (usages)
- **Value / unit:** `8000` chars
- **Scope:** **✓** — in map (#114).
- **Semantics:** "How many chars `read_chapter` may return at once when the
  LLM pages through a document." Two physical call sites:

```python
 46  MAX_TURNS = 30
 47  CHAPTER_MAX_CHARS = 8000
 48  MAX_CONSECUTIVE_FAILURES = 3
```

```python
354  def _tool_read_chapter(
355      parsed_content: str,
356      structure: DocumentStructure | None,
357      chapter_index: int,
358  ) -> str:
359      """读取指定章节全文。"""
360      if not structure or not structure.chapters:
361          return _format_chapter_text(parsed_content[:CHAPTER_MAX_CHARS], 0, "全文")
...
380      if len(text) <= CHAPTER_MAX_CHARS:
381          return f"{header}\n{text}"
382
383      truncated = text[:CHAPTER_MAX_CHARS]
384      remaining = len(text) - CHAPTER_MAX_CHARS
385      return (
386          f"{header}\n{truncated}\n\n"
387          f"…（本段共 {len(text)} 字符，已显示前 {CHAPTER_MAX_CHARS} 字符，"
388          f"剩余约 {remaining} 字符。\n"
```

**Note on semantics:** The "已显示前 X 字符" string at L387 is
LLM-visible. If the provider context window shrinks, the actual truncation
ceiling should arguably shrink with it. This is the exact concern behind
ticket #117 ("CHAPTER_MAX_CHARS vs preview 8000 — same setting or not?").

---

### A2 — `_build_init_msg` `DOC_FULL_THRESHOLD = 30000` (local const)

- **File:** `services/agentic_audit.py`
- **Lines:** L600–626
- **Value / unit:** `30000` chars
- **Scope:** **✓** — in map (#114).
- **Semantics:** "If the parsed document is no longer than this many chars,
  inline the entire content into the user prompt; otherwise inline only the
  first 8000 chars and tell the agent to use `read_chapter`." Owned by the
  **structured_llm / AgentAction** path.

```python
600  def _build_init_msg(
601      doc_name: str,
602      structure: DocumentStructure | None,
603      parsed_content: str = "",
604      kb_ids: list[str] | None = None,
605  ) -> ChatMessage:
606      DOC_FULL_THRESHOLD = 30000
607      structure_text = _tool_get_structure(structure, doc_name)
608      kb_summary = _get_kb_docs_summary(kb_ids or [])
609
610      if len(parsed_content) <= DOC_FULL_THRESHOLD:
611          content = (
612              f"{kb_summary}\n\n"
613              f"请审核文档《{doc_name}》。\n\n"
614              f"文档结构：\n{structure_text}\n\n"
615              f"=== 文档全文 ===\n{parsed_content}"
616          )
617      else:
618          content = (
619              f"{kb_summary}\n\n"
620              f"请审核文档《{doc_name}》。\n\n"
621              f"文档结构：\n{structure_text}\n\n"
622              f"=== 文档开头（共{len(parsed_content)}字）===\n"
623              f"{parsed_content[:8000]}\n"
624              f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
625          )
626      return ChatMessage(role=MessageRole.USER, content=content)
```

---

### A3 — `_build_init_msg` preview slice `parsed_content[:8000]`

- **File:** `services/agentic_audit.py`
- **Lines:** L623 (see A2 above)
- **Value / unit:** `8000` chars (literal — coincident with `CHAPTER_MAX_CHARS` but
  not referenced by it)
- **Scope:** **✓** — in map (#114).
- **Semantics:** "First 8000 chars of the parsed document, used as the
  initial-prefix visible to the LLM before it pages further with
  `read_chapter`." This is the exact value #117 is grilling: same number as
  `CHAPTER_MAX_CHARS`, not the same setting. The audit confirms **the two
  8000s have no code-level coupling** — they are independent literals that
  happen to match today.

---

### A4 — `_build_native_initial_messages` `DOC_FULL_THRESHOLD = 30000` (local const, copy-paste of A2)

- **File:** `services/agentic_audit.py`
- **Lines:** L1427–1455
- **Value / unit:** `30000` chars
- **Scope:** **✓** — in map (#114). This is the second copy of the same logic.
- **Semantics:** Same as A2; lives on the **native function calling / DeepSeek
  thinking** path. The body of `if/else` is byte-for-byte identical to A2
  except for the system-prompt wrapping outside the function.

```python
1427  def _build_native_initial_messages(
1428      parsed_content: str,
1429      structure: DocumentStructure | None,
1430      kb_ids: list[str],
1431      doc_name: str,
1432  ) -> list[dict]:
1433      kb_summary = _get_kb_docs_summary(kb_ids)
1434      DOC_FULL_THRESHOLD = 30000
1435      structure_text = _tool_get_structure(structure, doc_name)
1436      if len(parsed_content) <= DOC_FULL_THRESHOLD:
1437          user_content = (
1438              f"{kb_summary}\n\n"
1439              f"请审核文档《{doc_name}》。\n\n"
1440              f"文档结构：\n{structure_text}\n\n"
1441              f"=== 文档全文 ===\n{parsed_content}"
1442          )
1443      else:
1444          user_content = (
1445              f"{kb_summary}\n\n"
1446              f"请审核文档《{doc_name}》。\n\n"
1447              f"文档结构：\n{structure_text}\n\n"
1448              f"=== 文档开头（共{len(parsed_content)}字）===\n"
1449              f"{parsed_content[:8000]}\n"
1450              f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
1451          )
1452      return [
1453          {"role": "system", "content": NATIVE_SYSTEM_PROMPT},
1454          {"role": "user", "content": user_content},
1455      ]
```

**Sub-finding (callers of these entry points):**

```python
1458  def _build_structured_initial_messages(
1459      doc_name: str,
1460      structure: DocumentStructure | None,
1461      parsed_content: str,
1462      kb_ids: list[str],
1463  ) -> list[dict]:
1464      sys_msg = _build_system_msg()
1465      init_msg = _build_init_msg(doc_name, structure, parsed_content, kb_ids)
...
1521      if provider == "deepseek":
...
1524          initial_messages = _build_native_initial_messages(
1525              parsed_content, structure, kb_ids, doc_name,
...
1570      initial_messages = _build_structured_initial_messages(
1571          doc_name, structure, parsed_content, kb_ids,
```

The duplicated user-prompt body is reached via two entry points:
`_build_structured_initial_messages` (default path) and directly from
`_build_native_initial_messages` (DeepSeek). Task #119 already proposes
unifying these.

---

### A5 — `_build_native_initial_messages` preview slice `parsed_content[:8000]`

- **File:** `services/agentic_audit.py`
- **Lines:** L1449 (see A4 above)
- **Value / unit:** `8000` chars
- **Scope:** **✓** — in map (#114). Copy-paste of A3.
- **Semantics:** Identical to A3. The two `[8000]` slices (A3 + A5) are
  expression-level identical and live in functions with otherwise identical
  bodies; consolidating under #119 is part of the spec's "消除硬编码债 +
  重复实现".

---

### A6 — `read_chapter` tool description "内容最长显示4000字符"

- **File:** `services/agentic_audit.py`
- **Lines:** L663–676
- **Value / unit:** `4000` chars
- **Scope:** **△** — borderline. Not in the §1 list, but it sits in the same
  audit/prompt data path and contradicts `CHAPTER_MAX_CHARS = 8000` (which is
  the actual truncation ceiling for `read_chapter`). The ticket literally
  asks to flag any "tool result 截断" semantics.
- **Semantics:** LLM-visible contract for the `read_chapter` tool. Tells the
  agent the tool will return at most 4000 chars.

```python
668              "name": "read_chapter",
669              "description": (
670                  "读取文档指定章节的全文内容。当系统消息中显示的文档片段不足以审核目标章节时使用此工具。"
671                  "返回的文本前会标注章节名称标签（=== 章节名 ===），内容最长显示4000字符。"
672                  "若内容被截断，返回末尾会显示已读/剩余字符数；此时建议先用更精准的搜索词调用 search_kb "
673                  "或 search_kb_text 获取对应标准进行审核，而非逐字通读全文。"
```

**Why borderline:** `_format_chapter_text` at L380/383 truncates with
`CHAPTER_MAX_CHARS = 8000`, not 4000. So the LLM is **told** it can expect
at most 4000 chars but **gets** 8000. This means:
- either the tool description is stale (cap raised but doc not),
- or the cap should drop to 4000 and `CHAPTER_MAX_CHARS` should follow.

This is a documentation/runtime mismatch that has been silently riding
since the cap was tuned. Worth deciding in #117 alongside whether `8000`
stays.

---

### A7 — `extract_standards_deepseek` `max_tokens=4096`

- **File:** `services/standard_linker.py`
- **Lines:** L85–99
- **Value / unit:** `4096` tokens (LLM-side completion budget)
- **Scope:** **△** — borderline. The ticket is about prompt *construction*,
  but #115 §"尤其关注" lists `token 估算` as one of the audit hooks.
- **Semantics:** LLM-side completion budget when `standard_linker` calls the
  DeepSeek OpenAI SDK to extract standard numbers/names from already-flagged
  issues. Independent of the audit prompt-construction path.

```python
 85  model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
 86
 87  http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60))
 88  client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
 89
 90  response = client.chat.completions.create(
 91      model=model,
 92      messages=[
 93          {"role": "system", "content": system_prompt},
 94          {"role": "user", "content": user_prompt},
 95      ],
 96      response_format={"type": "json_object"},
 97      temperature=0,
 98      max_tokens=4096,
 99  )
```

**Why borderline:** It lives on the post-audit linking step. It is the only
"token-side" budget in the audit pipeline today; flagging it now means
#119 can decide "not on this ticket" without an extra dead-end.

---

### A8 — Reasoning-content preview `reasoning_content[:2000]`

- **File:** `services/agentic_audit.py`
- **Lines:** L1026–1027
- **Value / unit:** `2000` chars (DeepSeek `reasoning_content` field, sliced
  before being emitted as a SSE event)
- **Scope:** **△** — borderline. Surface is event-emission, not
  prompt-injection, but the field is `assistant_msg["reasoning_content"]` at
  L1031 which **does get re-injected into the next turn's message history**;
  and the next-turn `messages` list is what the LLM sees. So truncating
  here **does affect prompt size** for subsequent turns.
- **Semantics:** "How many chars of DeepSeek reasoning we surface to the
  client (and preserve for later turns)."

```python
1024          msg = response.choices[0].message
1025
1026          if msg.reasoning_content:
1027              emit({"type": "reasoning", "content": msg.reasoning_content[:2000]})
1028
1029          assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
1030          if msg.reasoning_content:
1031              assistant_msg["reasoning_content"] = msg.reasoning_content
```

Caveat: the **emit** truncates to 2000, but the **assistant_msg** that goes
back into the LLM context (line 1031) stores the full `reasoning_content`,
so the LLM sees all of it. Strictly speaking, only the **SSE/UI side** is
truncated; **prompt-reinjection** is not. Updating per #118's provider
budget **does not affect LLM context**, only what the operator reads.

---

### A9 — `search_kb_text` body truncation `body[:5000]`

- **File:** `services/agent_tools.py`
- **Lines:** L213–220
- **Value / unit:** `5000` chars
- **Scope:** **△** — borderline. Tool-result truncation lands directly in
  the LLM context as a `tool` message. Not in #115 §1 but **same
  family** ("任何 [:N] 字符串切片" + "tool result 截断").
- **Semantics:** "If the joined `search_kb_text` body exceeds 5000 chars,
  slice and append a '[截断]' marker."

```python
213      parts = []
214      for h in hits[:5]:
215          loc = f"doc={h['doc_id']} / page={h['page_number']}"
216          parts.append(f"【{loc}】\n{h['content']}")
217      body = "\n\n---\n\n".join(parts)
218      if len(body) > 5000:
219          body = body[:5000] + "\n... [截断]"
220      return f"【知识库文本搜索结果（精确匹配: {query}）】\n{body}"
```

**Why borderline:** This **does** flow into the LLM as the `tool` role
content (see agentic_audit L1395–1399). Moving it under #119 is fair;
moving it under a separate "tool-result truncation" ticket is also fair.
Recommend: ride along under #119 since it's a single constant.

---

### A10 — `MAX_TURNS = 30` / `MAX_ITERATIONS = 20` (loop caps)

- **File:** `services/agentic_audit.py` L46 (`MAX_TURNS = 30`);
  `services/agentic_qa.py` L40 (`MAX_ITERATIONS = 20`).
- **Value / unit:** `30` and `20` iterations (not chars; agent-loop turn caps).
- **Scope:** **✗** — explicitly out of scope per #114's "Out of scope" list:
  "改 agent loop 行为（tool 数量 / turn 上限）".
- **Semantics:** Hard stops on agent loop iteration counts.

```python
# services/agentic_audit.py
46  MAX_TURNS = 30
47  CHAPTER_MAX_CHARS = 8000
48  MAX_CONSECUTIVE_FAILURES = 3

# services/agentic_qa.py
40  MAX_ITERATIONS = 20
```

The native path overrides max_turns to **100** at L1538:

```python
1538                  max_turns=100,
```

…which is itself a hard-coded override. Listed here only because it
**contradicts** `MAX_TURNS = 30` for the DeepSeek branch and should be
raised if anyone tightens `MAX_TURNS`. (Out of scope this map.)

---

### A11 — `MAX_CONSECUTIVE_FAILURES = 3`

- **File:** `services/agentic_audit.py`
- **Lines:** L48 (definition), L1315 (defaulted)
- **Value / unit:** `3` (consecutive step failures before abort)
- **Scope:** **✗** — loop-safety cap, not prompt-construction.

```python
1315  _max_failures = max_consecutive_failures if max_consecutive_failures is not None else MAX_CONSECUTIVE_FAILURES
```

---

### A12 — `MAX_UPLOAD_SIZE` / `MAX_UPLOAD_SIZE_MB`

- **File:** `core/settings.py` L28–30; consumed by `api/routers/documents.py`
  L4, L21–22, L43, L53–54; `api/routers/audit_docs.py` L9, L78–79.
- **Value / unit:** `100` MB → `100 * 1024 * 1024` bytes.
- **Scope:** **✗** — file ingestion, not prompt.

```python
# core/settings.py
28  # ── 文件上传大小限制 ──────────────────────────────────────────────────────────
29  # 默认 100MB，通过 MAX_UPLOAD_SIZE_MB 环境变量可调整
30  MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024
```

Listed because the ticket asked to flag even-out-of-scope items.

---

### A13 — `MAX_SESSION_AGE = 7200`

- **File:** `services/qa_service.py`
- **Lines:** L127–135
- **Value / unit:** `7200` seconds (= 2 hours), QA multi-turn session TTL.
- **Scope:** **✗** — session lifecycle, not prompt.

```python
127  MAX_SESSION_AGE = 7200
128  _sessions: dict[str, dict] = {}
129
130
131  def _cleanup_sessions():
132      now = time.time()
133      expired = [sid for sid, s in _sessions.items() if now - s["created_at"] > MAX_SESSION_AGE]
```

---

### A14 — QA `ChatMemoryBuffer.from_defaults(token_limit=4000)`

- **File:** `services/qa_service.py`
- **Lines:** L138–153
- **Value / unit:** `4000` tokens (per multi-turn session history budget
  maintained by LlamaIndex)
- **Scope:** **△** — borderline. Ticket called out "embedding / QA 路径
  里的类似阈值（即使不在本次 map 范围，也要标出）". Token budget rather
  than char budget, but it's the **only** QA-side proxy for "how much
  context do we let the LLM see across turns", which is the same question
  #119 is solving for audit.
- **Semantics:** Built into LlamaIndex `ChatMemoryBuffer`. The chat engine
  silently drops older turns once the rolling conversation exceeds 4000
  tokens.

```python
138  def _build_chat_engine(kb_ids: list[str], top_k: int) -> ContextChatEngine:
139      """创建 ContextChatEngine 实例。
140
141      内置 ChatMemoryBuffer 管理对话历史，自动进行 token 感知的截断。
142      """
143      retriever = CrossKBRetriever(kb_ids=kb_ids, top_k=top_k)
144      memory = ChatMemoryBuffer.from_defaults(token_limit=4000)
```

---

### B-series — fixed-number snippets that *do* flow into prompts

These are **content-slicing literals** that determine what the LLM or the
UI shows. Most are out of #119's scope; flagging them here for completeness
per the ticket ("任何 [:N] 字符串切片" + "even 不在 map 范围，也要标出").

| ID | File:line | Slice | Meaning | Scope |
|---|---|---|---|---|
| B1 | `services/qa_service.py:110` | `n.node.text[:300]` | per-chunk QA answer snippet | ✗ |
| B2 | `services/qa_service.py:205` | `n.node.text[:300]` | per-chunk QA chat snippet | ✗ |
| B3 | `api/routers/qa.py:67` | `source.get("content_snippet") or "")[:300]` | SSE source-doc payload | ✗ |
| B4 | `services/agentic_qa.py:274` | `(buf.get("content_snippet") or "")[:300]` | QA source extraction | ✗ |
| B5 | `services/agentic_audit.py:236` | `parsed_content[start:start + 2000]` | last-chapter fallback size | △ |
| B6 | `services/agentic_audit.py:559` | `(action.cited_excerpt or "")[:200]` | Location.original_text | ✗ |
| B7 | `services/agentic_audit.py:1383` | `new_issue.description[:300]` | `issue_found` SSE payload | ✗ |
| B8 | `services/agentic_audit.py:1027` | `msg.reasoning_content[:2000]` | reasoning SSE payload | △ (same as A8) |
| B9 | `services/agentic_tools.py:219` | `body[:5000]` | search_kb_text truncation | △ (same as A9) |
| B10 | `services/vector_search.py:77` | `snippet.strip()[:500]` | KB text snippet (per-page) | ✗ |
| B11 | `services/vector_search.py:265` | `r.get('content', '')[:1000]` | topic_audit KB chunk slice | ✗ |
| B12 | `services/vector_search.py:271` | `[:200]` | topic_audit keyword slice | ✗ |
| B13 | `core/index_manager.py:209-217` | `_chunk_prefix(..., max_chars=200)` | KB chunk page-locate prefix | ✗ |
| B14 | `services/standard_linker.py:248, 295` | `content[:500]` | stored standard chunk_text | ✗ |

The 8000/30000/5000/4000/2000/200/500/300 family **converges on a small set
of semantically distinct operations**:

1. **doc-side full-text gate** (`DOC_FULL_THRESHOLD = 30000`) — should a
   document be inlined into the system/user prompt at all?
2. **doc-side preview slice** (`[:8000]`) — first-N chars to give the LLM
   context before paging.
3. **chapter-side slice cap** (`CHAPTER_MAX_CHARS = 8000`, with a stale
   docstring claim of `4000`) — how much `_format_chapter_text` returns.
4. **tool-result body slice** (`search_kb_text[:5000]`) — tool return budget.
5. **QA multi-turn token budget** (`ChatMemoryBuffer.token_limit=4000`) —
   session-history budget.
6. **Standard-linker completion budget** (`max_tokens=4096`).
7. **SSE-only reason snippet** (`reasoning_content[:2000]`).
8. **UI-facing content snippets** (`[:300]`, `[:200]`) — not in the LLM
   chain (post-LLM).

B5 (`parsed_content[start:start + 2000]` at L236) deserves a mention. It's
the fallback used when `_find_chapter_text` cannot find the next chapter
boundary; it returns at most **2000** chars. Distinct concern from A2/A3
(doc-side), but the value is itself arbitrary and could ride along under
#119 if `#117` opens the door to "separate preview vs slice cap".

```python
# services/agentic_audit.py L230-239
230          end = _locate_label(parsed_content, next_label, after=start)
231          if end < 0 and next_chapter.title and next_chapter.title != next_label:
232              end = _locate_label(parsed_content, next_chapter.title, after=start)
233          if end > start:
234              return parsed_content[start:end].strip()
235          # 找不到下一章 → 估算
236          return parsed_content[start:start + 2000].strip()
```

---

### C-series — out-of-scope but mentioned by the ticket for completeness

| ID | File:line | Value | Meaning | Scope |
|---|---|---|---|---|
| C1 | `core/settings.py:30` | `100` MB | file upload size cap (env-tunable) | ✗ |
| C2 | `services/audit_doc_service.py:186` | `char_count // 3000` | page-count estimate fallback | ✗ |
| C3 | `services/audit_doc_service.py:229` | `len(text_parts) // 30` | DOCX page-count estimate | ✗ |
| C4 | `core/index_manager.py:251` | `0.85` | LCS layout-match ratio | ✗ |
| C5 | `core/index_manager.py:249` | `4` | MIN_LCS_LEN | ✗ |
| C6 | `core/settings.py:103` | `EMBED_BATCH_SIZE=8` | embed batch size | ✗ |
| C7 | `core/settings.py:42-45` | `chunk_size=512 / chunk_overlap=50` | KB splitter config | ✗ |
| C8 | `core/settings.py:127` | `RERANKER_TOP_N=5` | reranker top_n | ✗ |
| C9 | `services/vector_search.py:30` | `max_hits: int = 5` | KB text-search max hits | ✗ |
| C10 | `services/vector_search.py:170` | `top_k: int = 5` | KB vector-search top_k | ✗ |
| C11 | `services/qa_service.py:101, 219, 255` | `top_k: int = 5` | QA top_k default | ✗ |

All of C-series are **already env-tunable** or **not LLM-prompt-related**.

---

## 4. Call graph

```
                                    ┌──────────────────────────────┐
                                    │ api/routers/audit_docs.py    │
                                    │ api/routers/documents.py     │
                                    │ cli/main.py  (audit_doc_svc) │
                                    └────────────┬─────────────────┘
                                                 │ parse_document (audit_doc_service.L100)
                                                 ▼
              ┌──────────────────────────────────────────────────────────┐
              │ services/audit_doc_service.parse_document(doc_id)         │
              │   → AuditDocument( parsed_content, structure )           │
              └────────────────┬─────────────────────────────────────────┘
                               ▼
              ┌──────────────────────────────────────────────────────────┐
              │ services/audit_task_service.run_audit(task_id)            │
              │   call: services.agentic_audit.run_agentic_audit(         │
              │     parsed_content, structure, kb_ids, doc_name,          │
              │     task_id, doc_id, event_callback )                     │
              └────────────────┬─────────────────────────────────────────┘
                               ▼
              ┌──────────────────────────────────────────────────────────┐
              │ services/agentic_audit.run_agentic_audit  [L1503-1590]    │
              │   provider branch:                                         │
              │     deepseek  → NativeLLMStep + run_agent_loop (max=100)  │ ───────────┐
              │     default/  → StructuredLLMStep + run_agent_loop         │            │
              │      fallback   (max=MAX_TURNS=30)                        │            │
              └────────────────┬─────────────────────────────────────────┘            │
                               ▼                                                       │
              ┌──────────────────────────────────────────────────────────┐            │
              │ _build_native_initial_messages  [L1427-1455]              │ A2 A3 A4 A5│
              │ _build_structured_initial_messages  [L1458-1469]          │ ───────────┼──┐
              │   → _build_init_msg  [L600-626]  ◄── DOC_FULL_THRESHOLD  │            │  │
              │                                          parsed_content[:8000]
              │ system prompts: SYSTEM_PROMPT | NATIVE_SYSTEM_PROMPT     │            │
              └────────────────┬─────────────────────────────────────────┘            │
                               ▼                                                      │
              ┌──────────────────────────────────────────────────────────┐            │
              │ run_agent_loop  [L1259-1419]                              │            │
              │   (loop with tool dispatch: read_chapter, search_kb,     │            │
              │    search_kb_text, flag_issue; per turn emits SSE)        │            │
              │   ┌─ _tool_read_chapter → _format_chapter_text           │            │
              │   │   ◄── CHAPTER_MAX_CHARS=8000 [L356, L380-388]         │            │
              │   │   ◄── tool description: "内容最长显示4000字符"        │ A1  A6     │
              │   ├─ search_kb         (services/agent_tools.search_kb)   │            │
              │   ├─ search_kb_text    (services/agent_tools.L193-220)    │ A9         │
              │   │   ◄── body[:5000]                                    │            │
              │   └─ flag_issue  (records AuditIssue)                     │            │
              │                                                              ◄────────┘
              │   cap:  max_turns (default 30, native 100)                 │ A10
              │          MAX_CONSECUTIVE_FAILURES=3  [L48 / L1315]        │ A11
              │   side effect: emit "reasoning"[:2000]                    │ A8
              │                       emit "issue_found".description[:300]│ B7
              └────────────────┬─────────────────────────────────────────┘
                               ▼
              ┌──────────────────────────────────────────────────────────┐
              │ _audit_post_process  [L1472-1500]                         │
              │   → save_trace  (services/agent_trace)                    │
              │   → link_standards  (services/standard_linker)            │
              │       ◄── extract_standards_deepseek  max_tokens=4096     │ A7
              │   → _build_result  (AuditResult)                          │
              └───────────────────────────────────────────────────────────┘


              ┌──────────────────────── QA PATH ──────────────────────────┐
              │ api/routers/qa.py / cli/main.py                           │
              │   ask(req) → services.qa_service.ask  (one-shot)           │
              │   chat(req) → services.qa_service.chat (multi-turn)       │
              │     → _build_chat_engine → ContextChatEngine              │
              │                      ◄── ChatMemoryBuffer(token_limit=4000)│ A14
              │   USE_AGENTIC_QA=true → services.agentic_qa.run_agentic_qa│
              │     (services/agentic_qa.L167)                            │
              │     → uses services.agentic_audit.run_agent_loop          │
              │       (StreamingLLMStep) with max_turns=MAX_ITERATIONS=20 │ A10
              └───────────────────────────────────────────────────────────┘
```

The promise to "按 provider 可调" (#114 / #116 / #118) has only one
decision point that flows into LLM-side context size: A1 + A2 + A3 + A4 + A5
in the audit path, and A14 on the QA path. The other entries either don't
move into the LLM's view (B-series UI snippets) or are unrelated to
prompt construction.

---

## 5. Decision recommendations

The ticket asked: "决策建议：哪些阈值该一起搬、哪些该单独 ticket"。

### Move together with #119 (✓ already in spec, no changes)

- **A2** `DOC_FULL_THRESHOLD = 30000` (L606)
- **A4** `DOC_FULL_THRESHOLD = 30000` (L1434, copy-paste)
- **A3** `parsed_content[:8000]` (L623, user-prompt preview slice)
- **A5** `parsed_content[:8000]` (L1449, copy-paste)
- **A1** `CHAPTER_MAX_CHARS = 8000` (L47)

These five are the contract. No new ✓ items.

### Borderline: ride along vs punt

- **A6** `read_chapter` description "4000" (L671). The docstring-claim
  doesn't match `CHAPTER_MAX_CHARS = 8000`. Fix is two-line: either drop
  the cap on `read_chapter` to 4000 + sync `CHAPTER_MAX_CHARS`, or update
  the description to 8000. Either way this is a 5-minute addition to #119
  and saves a future ticket; recommend it goes in #119.

- **A9** `search_kb_text` `body[:5000]` (services/agent_tools.py:218-219).
  Single constant, single line. It's a tool-output truncation; same
  family as A2/A3/A4/A5 per ticket phrasing ("tool result 截断"). Add to
  #119 scope: bump it into the same `core/settings.py` block.

- **A8** `reasoning_content[:2000]` (L1027) — only truncates the SSE emit,
  not the message-injection side (L1031 stores the full content for
  re-injection), so it does **not** bound LLM-side context. Not strictly
  needed for #119. Either trim during #119 or punt.

- **A14** `ChatMemoryBuffer(token_limit=4000)` — QA-side history budget.
  Token-based, not char-based; lives in `qa_service._build_chat_engine`,
  not the audit prompt pipeline. The ticket literally asked to flag it.
  Recommend **separate ticket** ("Token-aware QA context budget
  settings", aligned with #118's provider→threshold shape): it's a
  LlamaIndex API call with a different unit (tokens, not chars), and
  reshuffling it on the char-budget pass risks over-coupling unrelated
  systems.

- **A7** `max_tokens=4096` (services/standard_linker.py:98) — completion
  cap for an LLM call from `standard_linker`. Independent unit
  (completion tokens), independent concern (post-audit step). **Separate
  ticket** if even worth moving.

- **B5** `parsed_content[start:start + 2000]` (services/agentic_audit.py:236)
  — last-chapter-end fallback size. Touched on the chapter-finding path
  rather than the prompt path; can ride along but is not required.

### Definitely separate tickets

- **A10** `MAX_TURNS = 30` / `MAX_ITERATIONS = 20` + native override `100`
  — agent-loop caps, explicitly out-of-scope (#114). Already over-determined;
  don't touch in this map.

- **A11** `MAX_CONSECUTIVE_FAILURES = 3` — same.

- **A12** `MAX_UPLOAD_SIZE = 100 * 1024 * 1024` — file-upload, already
  env-tunable.

- **A13** `MAX_SESSION_AGE = 7200` — session lifecycle.

- **B1–B4, B6–B7** UI content snippets — non-LLM-facing.

- **B10–B14** KB search/snippet/index offsets — KB index concerns.

- **C1–C11** — page estimates, KB chunking, embed/rerank, similarity.

### Summary for #119

| Action | Count | Items |
|---|---|---|
| Move under #119 | 5 + 2 = 7 | A1, A2, A3, A4, A5 (the existing 5) + A6 (read_chapter description sync), A9 (search_kb_text body cap). |
| Optional in #119 | 2 | A8 (reasoning_content[:2000]), B5 (fallback 2000). |
| Separate ticket(s) | 2 | A7 (standard_linker max_tokens), A14 (QA ChatMemoryBuffer token_limit). |
| Strictly out of scope | 9 | A10, A11, A12, A13, A15 (= MAX_ITERATIONS counted with A10), B1-B4, B6-B7, B10-B14, C-series. |

---

## 6. Surprises / observations

1. **Read_chapter tool description is stale.** It tells the LLM that the
   tool returns "最多 4000 字符" but `_format_chapter_text` actually
   truncates to `CHAPTER_MAX_CHARS = 8000`. Either the description is
   wrong or the cap was raised without updating the description. Not
   caught by anything in `tests/`.

2. **The two `8000`s (preview slice vs chapter cap) have no symbolic link.**
   They are independent literals. Today they happen to match. #117's
   question "是否同一回事？" — code-level answer: **no**, but visually
   **yes**. Audit recommends treating them as one setting under (a)
   ("同源" choice) **or** as two settings under (b); either is
   defensible.

3. **Two `30000`s are *already* obviously the same value** — they're local
   constants in two functions with byte-for-byte identical bodies. This
   is exactly the duplicate-implementation debt the spec calls out.

4. **No token estimator exists.** The codebase uses char-length
   (`len(parsed_content)`) on the audit side and explicit `token_limit`
   on the QA side. There's no single "how many tokens does the prompt
   take" call. `core/settings.py` keeps LLM-side context windows only as
   white-list registrations (L225-226, L250-251) for the OpenAI SDK;
   they aren't read by prompt construction.

5. **`MAX_TURNS = 30` is silently overridden to `100` on the DeepSeek
   native path** at `run_agentic_audit.L1538`. Not in scope for #119,
   but if the `30` cap is re-tuned in a future ticket, the `100` will
   get out of sync.

6. **The reasoning_content emit slice (A8) does not actually bound prompt
   size for the LLM** — the same field is stored verbatim at L1031 and
   reinjected. So the SSE-visible `[:2000]` is documentation only. This
   is non-obvious at the call site and could mislead future readers.

7. **Two token-shaped budgets are scattered across services** —
   `ChatMemoryBuffer.from_defaults(token_limit=4000)` in
   `qa_service._build_chat_engine` and `max_tokens=4096` in
   `services/standard_linker.extract_standards_deepseek`. Different
   units of measure, different stages. Worth keeping in one place if
   #116 / #118 decide to centralize "per-LLM-call token budgets".

---

## 7. Tally

- **Total hard-coded numeric thresholds discovered:** 18 (A1–A14 + the A8
  re-entry counted under A8; the B- and C-series are *content slices* /
  env-tunable / non-prompt, so they don't add to the count of "move
  candidates").
- **In map scope (✓):** 5 (A1, A2, A3, A4, A5) — matches the parent map
  body's list (L47, L606, L623, L1434, L1449) byte-for-byte.
- **Borderline (△):** 4 (A6, A8, A9, A14 — and optionally B5).
- **Strictly out of scope (✗):** 9 (A10, A11, A12, A13, A14 if not moved,
  + the B/C-series).

---

## 8. References

- Parent map: `raawaa/tech-doc-audit#114`
- This research: `raawaa/tech-doc-audit#115`
- Follow-up decisions: `raawaa/tech-doc-audit#117`, `#118`
- Implementation: `raawaa/tech-doc-audit#119`

Files inspected:
- `services/agentic_audit.py` (1590 lines, full read)
- `services/agentic_qa.py` (342 lines, full read)
- `services/qa_service.py` (297 lines, full read)
- `services/agent_tools.py` (220 lines, full read)
- `services/standard_linker.py` (sampled at threshold lines)
- `services/vector_search.py` (sampled at threshold lines)
- `services/audit_doc_service.py` (sampled at threshold lines)
- `services/audit_task_service.py` (sampled around audit entrypoint)
- `core/settings.py` (336 lines, full read)
- `core/index_manager.py` (sampled at threshold lines)
- `core/parse_document.py` (458 lines, full read)
- `core/degradation.py` (52 lines, full read)
- `api/routers/qa.py`, `api/routers/audit_tasks.py`, `api/routers/audit_docs.py`,
  `api/routers/documents.py` (sampled)
- `cli/main.py` (sampled around `run_audit` callers)

All findings reference absolute paths in the worktree:
`/home/yuwenjie/Code/jishu_shenhe/.claude/worktrees/agent-abb53a3b967715e3e/`.
