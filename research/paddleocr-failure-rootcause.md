# PaddleOCR 失败根因调研 — 虹桥公司制度 KB 2 篇

> **TL;DR**:
> Issue 报告的两篇 doc 在 2026-07-29 直接重 OCR PaddleOCR-VL-1.6 均成功 — Doc A 拿到 5 页 / 共 68 个 layout blocks、Doc B 拿到 3 页 / 共 36 个 blocks，所有 `block_bbox` 为合法整数、`block_content` 全部非空、`markdown.text` 每页 ≥ 474 字符。
> Issue 描述的"失败"在当前代码路径下 **不可复现**；残存的失败指纹只出现在 Doc B 的本地 `data/.cache/paddleocr/82fc816a…_PaddleOCR-VL-1.6.json`，`source=fallback_pdfplumber`、`full_text=""`、`layout_blocks=0`。
> 因此 issue 描述里"PaddleOCR-VL-1.6 模型在两份文字版 PDF 上能力失败"这一假说被 trace **证伪**；真实根因是历史 **PaddleOCR 状态机 → `_pdf_fallback()` 静默降级 → `embedding_status` 假成功**（缓存污染），属 #93 同源 bug 的延续，不是模型能力问题。
>
> **重要边界（evidence calibration）**：今天 trace 全 `done` ≠ 历史失败是 PaddleOCR credentials 问题；今天的成功只能证明 *服务端当前对这两 doc 健康*，不能证明过去失败没有网络/服务端瞬时问题。报告里凡"根因 = X"按"**已证 / 强推断 / 仅存可能**"三档标记。

---

## 0. status 校正（与上一版报告对齐用）

| 维度 | 旧版结论（paddleocr-failure-rootcause.md 2026-07-29 旧稿） | 本次核实（2026-07-29 11:20Z） | 备注 |
|---|---|---|---|
| Doc A `embedding_status` | `failed` | `embedded`（meta `data/kbs/01KW1PG49FQDAEYV0W1H2H309E/meta/01KW1R8ZV7FFADC3449K2JMQE5.json` 现读） | issue 描述基于 #90 跑出"1 failed"那一刻的快照，A 后来在 `2026-07-28T08:22:27+00:00` 被旁路重 OCR 救活 |
| Doc B `embedding_status` | `embedded`（issue 写 `failed`） | `embedded`（meta 现读） | 但 pages `top_level_layout=0`、cache `source=fallback_pdfplumber` → **假成功** |
| Doc A `cache.source` | `paddleocr`（旧版推断） | `paddleocr`（cache 现读 73,611 B） | 已证 |
| Doc B `cache.source` | `fallback_pdfplumber`（旧版推断） | `fallback_pdfplumber`（cache 现读 9,227 B，`parsed_at=2026-07-29T01:03:58.745553+00:00`） | 已证 |
| `_paddleocr_call` 是否带 orientation retry | "**当前 worktree 没有** empty retry 代码" | 实际 `core/parse_document.py:178-183` 存在：触发条件 `len(full_text) < 20` 后以 `orientation_classify=True` 重提 | 旧版描述与 canonical HEAD 不一致 — canonical HEAD 有这段代码 |
| issue 描述 `"PaddleOCR returned empty…"` 日志位置 | "调用栈已有但当前 worktree 没有" | `core/parse_document.py:181` 是 logger 输出位置 | 已定位 |

> 校正结论：旧版报告在 §1 / §3.4 关于"代码不存在 orientation retry"的描述需更正；本次以 canonical HEAD `core/parse_document.py` 为准。

---

## 1. 对照表（本次 trace vs 历史 cache 状态）

| 维度 | Doc A（治安保卫）`01KW1R8ZV7FFADC3449K2JMQE5` | Doc B（公司信息公开）`01KW1Q46DTADW9M0JHQ7EH6P4Y` | Ctrl（采购管理办法）`01KW1QYMMAQG851X4T92Z1DGHD` |
|---|---|---|---|
| 页数（pdfinfo） | **5** | **3** | — |
| 文件大小 | 497,983 B（498 KB） | 901,735 B（902 KB） | 274,075 B |
| sha256 | `8272d3d0…24ee` | `82fc816a…1198` | `fdd14106…1ec7` |
| PDF 类型（pdfinfo） | A4，未加密，PDF 1.3，`Page rot: 180` | A4，未加密，PDF 1.3 | — |
| 嵌入图片（pdfimages -list） | **5 张扫描位图**（jpeg 1240×1753 / ccitt 1657×2341 灰度 / …） | **0 张** | — |
| `pdftotext` 输出行数 | **0 行**（无内嵌文本） | **82 行**（含完整条款） | — |
| `cache.source` | `paddleocr`（73,611 B，2026-07-28T08:22:27Z） | `fallback_pdfplumber`（9,227 B，2026-07-29T01:03:58Z） | — |
| `cache.full_text` | ""（截断存） | ""（截断存） | — |
| `cache.layout_blocks` | 0（schema 顶层） | 0（schema 顶层） | — |
| `pages.json full_text` 长度 | 2626 | 1548 | — |
| `pages.json top_level_layout` | **5** | **0** | — |
| `meta.embedding_status` | `embedded` | `embedded`（**假成功**） | — |
| issue #94 报告时间点原始状态 | `embedding_status=failed` | `embedding_status=failed` | — |

**关键反差**：旧版报告说"两篇都健康"，但 **Doc B 在 pages 层仍然 layout=0** — 也就是说，B 是"假成功"。两篇状态不一致，不能简单一句"两篇都健康"。

---

## 2. PDF 特性诊断（Doc B 详细 / Doc A 对照）

> 本节为本次补做的诊断数据。旧版报告缺这一段。

### 2.1 `pdfinfo` 输出

| 字段 | Doc A | Doc B |
|---|---|---|
| Pages | 5 | 3 |
| Page size | 595 × 842 pts (A4) | 595.22 × 842 pts (A4) |
| Page rot | **180** | 0 |
| Encrypted | no | no |
| PDF version | 1.3 | 1.3 |
| File size | 497,983 B | 901,735 B |
| Optimized / Tagged / Form / JavaScript | no / no / none / no | no / no / none / no |

Doc A `Page rot: 180` 与历史 issue #94 注释里"`useDocOrientationClassify=False` 会让扫描件方向错"的观察一致 —— 但 PaddleOCR-VL-1.6 在本次 trace 中 **没有** 走 orientation retry（`full_text` 480/538/609/519/474 全部非空），所以对 A 来说 orientation 不再是 blocker。

### 2.2 `pdftotext` 输出（无 -layout）

- **Doc A**：`pdftotext … | wc -l` = **0**。文档不内嵌文本字符流。
- **Doc B**：`pdftotext … | wc -l` = **82**。含完整的"第一章 总则"、"第二章 信息公开内容"… "第五章 附则"章节，每条"第 X 条"标题 + 中文段落，与 issue 描述"文字版 PDF"完全一致。Doc B 抽出文字（节选前 6 行）：

```
公司信息公开管理暂行办法
            (ZD/BG-12-2023-B0)
               第一章 总     则
  第一条 为适应市场化、现代化管理需要，贯彻落实上海市国资委
和集团公司关于企业信息公开的指导意见和规定要求，进一步提高公
司信息公开的制度化、规范化水平…
```

### 2.3 `pdfimages -list`

- **Doc A**：每页 1 张光栅图（5 张），其中：
  - p1: rgb jpeg 1240×1753, 150 dpi, 211 KB（彩色封面 / 标题页）
  - p2-p4: gray ccitt 1657×2341, 200 dpi, ~30 KB（黑白扫描正文）
  - p5: rgb jpeg 1663×2344, 200 dpi, 183 KB（彩色封底）
  - 结论：**Doc A 是扫描件**（无文本层 + 全页位图）。这与 `pdftotext` 0 行吻合。
- **Doc B**：0 张光栅图。纯文字 PDF（与 `pdftotext` 82 行吻合）。

### 2.4 PaddleOCR 服务端本次 trace 的 per-page 实测（来自 `trace_Doc{B,A,Ctrl}.json`）

> 用户明确要求 "per-page comparison metrics (bbox None %, content empty %, markdown lengths)" — 这里给出。

| doc | page | markdown 字符数 | `parsing_res_list` blocks | bbox=None 块数 | content 为空块数 | width × height |
|---|---:|---:|---:|---:|---:|---:|
| DocA | 0 | 480 | 17 | 0 | 0 | 1190 × 1684 |
| DocA | 1 | 538 | 13 | 0 | 0 | 1192 × 1684 |
| DocA | 2 | 609 | 16 | 0 | 0 | 1194 × 1684 |
| DocA | 3 | 519 | 10 | 0 | 0 | 1192 × 1684 |
| DocA | 4 | 474 | 12 | 0 | 0 | 1196 × 1686 |
| DocA 合计 | — | **2620** | **68** | **0** | **0** | — |
| DocB | 0 | 512 | 12 | 0 | 0 | 1191 × 1684 |
| DocB | 1 | 515 | 14 | 0 | 0 | 1191 × 1684 |
| DocB | 2 | 515 | 10 | 0 | 0 | 1191 × 1684 |
| DocB 合计 | — | **1542** | **36** | **0** | **0** | — |
| Ctrl | 0–34 | 197–606（avg ≈ 522） | 5–17（avg ≈ 11） | 0 | 0 | 1190 × 1684 |

> 注：bbox None / content empty 比例对前 4 KB JSONL 头（`_paddleocr_repro._summarize_jsonl` 取的首块 `layoutParsingResults`）做语法截断解析得到 head 内 17 + 12 blocks 的 bbox 与 content 均非空；其余 page 数据来自服务端 response body 完整解析（见 `trace_*.json` 内 `pages[].pruned_blocks` 与 `md_text_len`）。DocA / DocB 合计 markdown 字符 2620 / 1542，与 `pages.json` full_text 长度 2626 / 1548 数量级一致，差值来自 heading processor 的多余空白处理。

**结论**：本次 trace 中 9 页（A 5 + B 3 + Ctrl 1 首页）里 **0 页** 出现 `bbox=None` 或 `content=""`，**0 页** markdown 字符数 < 100。**所有 bbox 都是合法正整数**，全部 `block_content` 非空。

### 2.5 PaddleOCR 服务端请求/响应核心字段

| doc | submit HTTP | submit elapsed | `jobId` | 首次轮询 `state` | 轮询 elapsed | JSONL HTTP |
|---|---|---:|---|---:|---:|---:|
| Doc A | 200 | 0.995 s | `75787207714566144` | `done` | 0.92 s | 200 |
| Doc B | 200 | 0.846 s | `75787236983717888` | `done` | 0.408 s | 200 |
| Ctrl | 200 | — | — | `done` | — | 200 |

请求体（与 `core/parse_document.py:194-203` 同源）：
```
POST https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
Authorization: bearer <PADDLEOCR_API_TOKEN>（len=40，前 6 / 后 4 字符略）
optionalPayload = {"useDocOrientationClassify": false, "useDocUnwarping": false, "useChartRecognition": false}
model = PaddleOCR-VL-1.6
```
**没有任何** 401 / 4xx / `state=failed` / 600 s timeout。trace 完整保存在 `/tmp/paddleocr-repro/{doc_id}_trace.json`。

---

## 3. 失败/成功 分类

按"模型能力 vs 工程链路"二维分类：

| doc | PDF 类型 | PaddleOCR 本次 trace | 本地 cache 当前状态 | 分类（结论） |
|---|---|---|---|---|
| **Doc A**（治安保卫） | 扫描件（5 页位图，PDF 1.3，无文本层） | done，5 页 68 blocks 全非空 | `paddleocr`（5 页 layout 完整） | **P1 — 当前健康**。原 issue 描述里的"failed"基于 #90 跑出的历史 snapshot；现在 A 已被旁路重 OCR 救活（cache `parsed_at=2026-07-28T08:22:27Z`）。 |
| **Doc B**（公司信息公开） | 文字版 PDF（82 行可抽文本，无图） | done，3 页 36 blocks 全非空 | `fallback_pdfplumber`，pages `top_level_layout=0`（**假成功**） | **P2 — 服务端健康 / 客户端缓存污染**。本次直接重 OCR 完全成功；但磁盘 cache 仍是 pdfplumber fallback，且 pages 层 layout=0，导致前端 chip 显示"未解析"。 |
| Ctrl（采购管理） | 文字版 PDF（35 页） | done，35 页全部正常 | 已知好 | 健康，参考基线。 |

Doc A / Doc B 服务端都健康 — **PaddleOCR-VL-1.6 对扫描件（A）和文字版（B）均无能力缺陷**。

---

## 4. Doc A 历史失败原因（按证据强度排序）

> 用户要求 section 4。Doc A 当前已经"绿"，但 issue 描述它曾 fail — 这里只把"历史上为什么 fail"按证据强度分级。

### 4.1 已证（proven）

- **2026-07-28T08:22:27Z 的旁路重 OCR**：cache 文件 `data/.cache/paddleocr/8272d3d0…_PaddleOCR-VL-1.6.json` 73,611 B、`source=paddleocr` — 大小比 Doc B 的 9,227 B 大一个数量级，符合"完整 blocks + polygons"形态（见 trace_DocA.json 内 page 0-4 widths 1190-1196、blocks 17/13/16/10/12）。
- **2026-07-28T08:26:57Z pages 落盘**：`pages.json` `top_level_layout=5`、`full_text_len=2626` — reparse pipeline 在 cache 写入之后正常完成。
- **`embedding_status="embedded"`**：meta 现读。

### 4.2 强推断（inferred，但有间接证据）

- **A 的 issue 描述 `failed` 基于 #90 跑出的"1 failed"快照**：bulk_reparse 当时缺 `load_dotenv()`（#93 复盘结论）→ 子进程 `PADDLEOCR_API_TOKEN`/`URL` 为空 → `_paddleocr_available()=False` → 走 `_pdf_fallback()` 留 `layout=[]` → `embedding_status` 仍被 `reparse_service` 设为 `embedded`（len(full_text)≥20 兜底未触发，因为 `_pdf_fallback()` 能从扫描件 OCR 抽到几百字符）。
- **`Page rot: 180` + `useDocOrientationClassify=False`** 是历史 fail 的 *可能* 触发条件之一，但本次 trace（同样 orientation_classify=False）拿到了完整 layout — 也就是即使历史上 orientation 是触发条件，今天已经自然恢复。

### 4.3 仅存可能（speculative）

- 服务端瞬时网络抖动 / 偶发 5xx：trace 没复现，且 issue #94 没有任何服务端 `errorMsg` 截图，纯属可能。
- 模型对扫描件中部分汉字识别失败：本次 trace 17/13/16/10/12 blocks 全非空、字符数均匀，**已证否**这一可能。

### 4.4 反证（disproven）

- "PaddleOCR-VL-1.6 模型对 Doc A 不行" — 本次 trace 拿到完整 5 页 68 blocks，**证伪**。

---

## 5. ranked 修复建议（按实施成本/收益比排序）

> 用户要求 section 5 "ranked fixes"。每条标注目标问题（H1-H6 与旧版报告命名一致）+ 证据强度 + 实施成本。

### Fix #1 — 【P0，立即】清 Doc B 污染 cache + 触发单篇 reparse（target: H1 假成功）

**证据**：trace 已证服务端当前对 B 正常；cache 现读 `source=fallback_pdfplumber`、`full_text=""`、`layout_blocks=0`。
**做法**：
```bash
rm /home/yuwenjie/Code/jishu_shenhe/data/.cache/paddleocr/82fc816a25a4486e3ed6b801ca5bfb321f28253221d08dd64a0b36efea921198_PaddleOCR-VL-1.6.json
# 然后
curl -X POST http://<api>/api/v1/kb-documents/01KW1Q46DTADW9M0JHQ7EH6P4Y/reparse
```
**收益**：直接消除 B 的假成功指纹，前端 chip 转绿。
**成本**：一次 reparse ≈ 数秒服务端算力。

### Fix #2 — 【P1，代码改动】`reparse_service` 加 layout 兜底（target: H1 根因）

**证据**：`services/reparse_service.py:77` 当前只校验 `len(full_text) < 20`，未校验 `layout=[]`，故 pdfplumber fallback 的"假成功"能漏过。
**做法**（最小补丁）：
```python
# services/reparse_service.py:75 之后
parse_result = parse_document(doc.file_path)
if not parse_result.full_text or len(parse_result.full_text) < 20:
    raise RuntimeError("parse_document returned empty/sparse text")
if not parse_result.layout:                       # ← 新增
    raise RuntimeError(f"parse_document returned empty layout (source=fallback) for {doc_id}")
```
**收益**：永久堵住"full_text 充足但 layout 空"的假成功（#93 同源 bug 死角的延伸）。
**成本**：1 行代码，零行为变化给正常 doc。

### Fix #3 — 【P1】`_paddleocr_call` 加 JSONL 空 / `state=failed` 的结构化 metric（target: H2 观测盲点）

**证据**：当前 `_paddleocr_call` (`core/parse_document.py:189-239`) 只有"timeout 600 s"一种终态，对 `state=failed` 只 `raise` 不分类 metric；对 `parsing_res_list=[]` 没有检测。
**做法**：在 `_paddleocr_call` 抛错前 append `_logger.error("paddleocr_observability: job_id=%s state=%s errorMsg=%s parsed_blocks=%s", …)`，并把 trace 写入 `data/.logs/paddleocr/{hash}.json` 便于事后聚合。
**收益**：未来再出现 issue 时不再只能凭 cache 倒推；定位时间从小时降到分钟。
**成本**：~15 行代码，无外部依赖。

### Fix #4 — 【P2，schema】新增独立 `layout_status` 字段（target: H3 `embedding_status` 语义过载）

**证据**：`embedding_status=embedded` 当前描述"已向量化"，但生产中拿它当"layout 完整"使用；B 的 meta 现读就是这种状态机的反例。
**做法**：meta JSON 新增 `"layout_status": "ok" | "empty" | "failed"`；`reparse_service` 在写 `embedded` 前先 assert `layout_status="ok"`；前端 chip 改读 `layout_status`。
**收益**：状态机自描述，再不会出现"embedded 但 layout 空"。
**成本**：≈ ADR-0004 取舍 1 的小例外（schema 加字段，不 backfill 存量 KB），需协调前端 + 索引服务。

### Fix #5 — 【P2】`_parse_pdf` 在 fallback 路径里不写 cache（target: H1 污染源）

**证据**：`core/parse_document.py:160-161` 一律 `save_cached(..., source=source)`，fallback 路径也写，导致下次 reparse 命中 stale cache。
**做法**：在 `_parse_pdf` 末尾判断 `if source == "fallback_pdfplumber": return result, "fallback_pdfplumber"` 不写 cache。
**收益**：下次 reparse 必然走 PaddleOCR，Doc B 这样的 fallback cache 不会再次污染。
**成本**：与 V8 defense 形成双保险；会让 `parse_document(use_cache=False)` 调用比例变高 → PaddleOCR quota 上升，需要看成本再决定。

### Fix #6 — 【P3】`_paddleocr_call` 单次重提（覆盖 H2 偶发失败）

**证据**：本次 trace 没复现服务端失败，但 issue #94 历史上 fail 可能是 H2。
**做法**：在 `state=failed` 时 `time.sleep(5)` 后单次重提一次 job（参数不变），二次仍 fail 才 raise。
**收益**：覆盖未来偶发 5xx。
**成本**：~10 行；与 Fix #3 一起做更经济。

---

## 引用

证据文件（全部在 `/tmp/paddleocr-repro/` 与项目 repo 内）：

- `/tmp/paddleocr-repro/01KW1R8ZV7FFADC3449K2JMQE5_trace.json` — Doc A PaddleOCR-VL-1.6 trace（按 doc_id 命名）
- `/tmp/paddleocr-repro/01KW1Q46DTADW9M0JHQ7EH6P4Y_trace.json` — Doc B PaddleOCR-VL-1.6 trace（按 doc_id 命名）
- `/tmp/paddleocr-repro/01KW1QYMMAQG851X4T92Z1DGHD_trace.json` — Ctrl PaddleOCR-VL-1.6 trace（参考基线）
- `/tmp/paddleocr-repro/jsonl_DocA.head.txt` / `jsonl_DocB.head.txt` — JSONL 前 4 KB for forensic
- `/tmp/paddleocr-repro/repro.py` — 复现脚本（提交 → 轮询 → GET JSONL，无 cache 写）
- `/home/yuwenjie/Code/jishu_shenhe/data/.cache/paddleocr/8272d3d0…24ee_PaddleOCR-VL-1.6.json`（73,611 B，Doc A 现状 `paddleocr`）
- `/home/yuwenjie/Code/jishu_shenhe/data/.cache/paddleocr/82fc816a…1198_PaddleOCR-VL-1.6.json`（9,227 B，Doc B 现状 `fallback_pdfplumber`，**待清理**）
- `/home/yuwenjie/Code/jishu_shenhe/data/kbs/01KW1PG49FQDAEYV0W1H2H309E/pages/01KW1Q46DTADW9M0JHQ7EH6P4Y.json`（Doc B pages `top_level_layout=0`）
- `/home/yuwenjie/Code/jishu_shenhe/data/kbs/01KW1PG49FQDAEYV0W1H2H309E/meta/01KW1R8ZV7FFADC3449K2JMQE5.json`、`meta/01KW1Q46DTADW9M0JHQ7EH6P4Y.json`（两篇 doc meta，`embedding_status=embedded`）
- `/home/yuwenjie/Code/jishu_shenhe/core/parse_document.py:178-186`（orientation retry 触发位置）、`:189-239`（`_paddleocr_call`）、`:398`（`_pdf_fallback`）、`:160-161`（cache 写入点）
- `/home/yuwenjie/Code/jishu_shenhe/services/reparse_service.py:74-77`（sparse-text guard）

外部参考：

- `pdfinfo / pdftotext / pdfimages`（poppler-utils）：本次直接在两篇 doc 上跑（见 §2）
- issue tracker：`raawaa/tech-doc-audit` #93（缓存污染根因）、#94（本次报告的 ticket）
- ADR：`docs/adr/` 下的 ADR-0004（schema 演进取舍，本次 Fix #4 涉及）
- PaddleOCR-VL-1.6 服务端 endpoint：`https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`（端点不展示 token；环境变量 `PADDLEOCR_API_TOKEN` 长度 40）

— research-agent, 2026-07-29 11:20Z
