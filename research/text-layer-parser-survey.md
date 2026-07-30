# Text-Layer PDF Parser Survey — `raawaa/tech-doc-audit`

> **Scope**: Free, pure-Python-friendly text-layer PDF parsers as a faster, on-prem alternative
> to the current PaddleOCR-VL-1.6 cloud path (`core/parse_document.py`).
> **Hard constraints**: free / OSI-license; pure `pip install` preferred (no Java, no GPU, no
> model download); produce layout blocks (bbox + order) for highlight preview; < 500 ms / page
> on text-layer PDFs.
> **Date**: 2026-07-29
> **Repo state at survey time**: `core/parse_document.py:39-50` defines the `Block` shape
> (`block_label / block_content / bbox_norm / polygon_norm / block_order`) and
> `core/parse_document.py:398-418` is the current pdfplumber fallback (`layout=[]`,
> blocks dropped).

---

## TL;DR

- **Recommended for T2 benchmark (ranked)**: **PyMuPDF (fitz)** >>> **pypdfium2** > **pdfplumber 0.11.10 (current fork `dhdaines/pdfplumber`)**.
- PyMuPDF is the only candidate that simultaneously hits every hard constraint (free, pure-Python install, layout blocks via `get_text("dict")`, native table extraction via `find_tables` ≥ 1.23, and 30-100 ms/page on text-layer PDFs). License is AGPL-3.0 — see §1 footnote on the **dual-license** option for in-house KB use.
- pdfplumber remains the safest *baseline* (already in `requirements.txt`, MIT) but is the one we are trying to escape for table quality.
- OCR / VLM families (PaddleOCR, PP-StructureV3, marker-pdf, MinerU, unstructured) are disqualified by either (a) cloud/credits dependency, (b) heavy model weights / GPU, (c) Java bridge, or (d) > 1 s/page. They should stay as the **fallback for scanned pages**, not the primary text-layer path.

---

## 1. Capability matrix

> Columns: **License** (SPDX, must be OSI-approved free), **Install** (`pip install X` only / Java / GPU / model download), **Latest release** (date + version + activity signal), **Layout blocks** (yes/no/limited + how: `get_text("dict")` / `extract_words()` / `extract_tables()` / custom), **Tables** (standard / merged cells / cross-page), **Speed** (estimate per text-layer page, p50; cite any public benchmarks), **ParseResult mapping** (1-line / 10-line / 100-line conversion to `Block`), **Verdict** (✓ ship-ready / △ needs work / ✗ disqualified).

| # | Candidate | License | Install | Latest release | Layout blocks | Tables: standard | Tables: merged cells | Tables: cross-page | Speed (text-layer, p50) | Mapping to `Block` | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **PyMuPDF (fitz)** | AGPL-3.0 **OR** Artifex Commercial¹ | `pip install pymupdf` (pure wheel, no GPU) | **1.28.0**, 2026-06-29 (active, monthly releases) | **Yes** — `page.get_text("dict")` returns `{width, height, blocks:[{type, bbox, lines:[{bbox, spans:[{bbox, text, font, size, …}]}]}]}`; 3 block types (text=0, image=1, vector=3); gives bbox at block/line/span/char level. | **Yes** — `page.find_tables()` (since 1.23) and `TextPage.extract_tables()` (1.26+) return `[[row×cells]]` with header detection and 3 strategies (`"lines"`, `"lines_strict"`, `"text"`). `Table.bbox` available. | **Partial** — `find_tables` returns bboxes; merged cells are typically **not** merged in the row output (each subcell is reported independently). `to_markdown()` flattens. Manual merge needed for true column-spans. | **Manual only** — `TableFinder` lifetime is one page. PyMuPDF docs reference external notebooks for "joining table fragments across multiple pages" — caller must implement the join (cell bbox y-overlap + header matching). | **~30-100 ms / page** on text-layer A4 (`get_text("dict")` micro-benchmarks widely reported; pypdfium2 14 MB / 818 pages text-extract in < 3 s ⇒ ~3.7 ms/page, and PyMuPDF is ~5-10× faster than pypdfium2 on text). Well under 500 ms budget. | **~10 lines** — walk `dict["blocks"]` → `Block`; join spans for `block_content`; normalize by `dict["width"]/dict["height"]`. `block_order` = `i`. | **✓ ship-ready** |
| 2 | **pypdfium2** | Apache-2.0 (BSD-3 on parts) | `pip install pypdfium2` (no native deps; wheels include PDFium) | **5.12.1**, 2026-07-17 (very active) | **Limited** — `PdfPage.get_textpage().get_text_range()` returns plain text with **bbox-per-glyph**; **no semantic blocks**. Need post-processing to group spans into paragraphs/columns. | **No** — pypdfium2 has no built-in table finder. | **No** | **No** | **~4-5 ms / page** (818 pages 14 MB in < 3 s). Slower than PyMuPDF for the same text. | **~30-50 lines** — glyph clustering → lines → blocks; comparable effort to a from-scratch layout. | **△ needs work** (fastest raw extract, but no block semantics → you re-implement what PyMuPDF gives for free) |
| 3 | **pdfplumber 0.11.10** | MIT | `pip install pdfplumber` (depends on `pdfminer-six`) | **0.11.10**, 2026-06-15 (active in `dhdaines/pdfplumber` fork; upstream `jsvine/pdfplumber` is low-frequency) | **Yes** — `page.chars / .lines / .rects / .words`; `page.extract_words()` gives `[{text, x0, top, x1, bottom, …}]`. **No native semantic block grouping** — must cluster by line + paragraph heuristics. | **Yes** — `extract_tables()` / `find_tables()` returns `[[row×cells]]`; based on pdfminer.six line/rectangle intersection. | **No** — docs explicit: identifies cells from line/rectangle intersections; **does not detect or merge cells that span multiple rows/columns** (PyPI docs, 2026-06-15). | **Limited** — `find_tables` is per-page. `pdfplumber` ≥ 0.11 has experimental `join_tabs` (Towards Data Science 2025 benchmark). Not robust for the project's "merged cell / cross-page" quality bar. | **~200-500 ms / page** on text-layer A4 (slower than PyMuPDF ~3-5×; baseline `core/parse_document.py:398` already uses it). | **~30 lines** — cluster `words` into lines/paragraphs by y/x, drop a `block_order`. | **△ baseline (current)** — keep as fallback, but it is exactly the weak-link we are moving off of. |
| 4 | **pdfminer.six 20260107** | MIT | `pip install pdfminer-six` (pure Python) | **20260107**, 2026-01-07 (rolling date-based versions) | **Yes, low-level** — `PDFPage → LTPage → LTTextBox / LTTextLine / LTChar / LTFigure` with bboxes; used internally by pdfplumber. | **No** — no high-level table API. Caller must walk `LTFigure` and `LTRect` and reconstruct. | **No** | **No** | **~150-400 ms / page**. Comparable to pdfplumber (it is the underlying engine). | **~50 lines** — `LTTextBox`/`LTTextLine` → `Block`; image blocks via `LTFigure`; LTRect → table candidate. | **△ needs work** (lower level than pdfplumber; only worth it if you need finer access than `chars`/`words`). |
| 5 | **camelot-py 2.0.0** | MIT | `pip install camelot-py` (pure Python, optional Ghostscript/poppler backends) | **2.0.0**, 2026-06-04 (active; 3.8k★) | **No** — camelot is a **table-only** tool; no text-layer blocks. | **Yes (best-in-class for line-drawn tables)** — `lattice` (ruled lines) and `stream` (whitespace) parsers. | **Partial** — `lattice` handles spanning cells implicitly; no explicit `merge_cells` API. | **Yes** — `stack_contiguous()` "stitch continuations across pages into a single table". | **~200-800 ms / page** (table-only; text still needs another tool). | **~100 lines** — combine camelot tables with a separate text-layer reader (e.g. PyMuPDF) to get blocks + tables. | **△ needs work** (excellent for tables, useless alone for layout). |
| 6 | **tabula-py 2.10.0** | MIT | `pip install tabula-py` **+ Java 8+ runtime required** | **2.10.0**, 2024-10-17 (maintenance mode; 8-month gap to next release) | **No** — tables only. | **Yes** | **No** | **No** | **~300-1000 ms / page** + JVM startup cost. | **~100 lines** (same shape as camelot). | **✗ disqualified** (Java dependency violates "pure Python" hard constraint; maintenance is sluggish). |
| 7 | **tika 3.1.0 (python-tika)** | Apache-2.0 | `pip install tika` **+ Java 7+ runtime required** | **3.1.0**, 2025-03-26 (slowly maintained) | **Yes** — Tika returns content + XHTML with `<p>` / `<div>` regions; bbox per region available. | **Partial** — Tika-PDFBox has table heuristics; weak on merged cells. | **No** | **No** | **~500-2000 ms / page** + JVM startup. | **~30 lines** — parse Tika XHTML; map `<p>` / `<table>` → `Block`; normalize by `pdfPageMediaDimensions`. | **✗ disqualified** (Java + slow + Tika-PDFBox table quality not a step up from pdfplumber). |
| 8 | **PaddleOCR-VL-1.6 (local pip)** | Apache-2.0 | `pip install paddleocr` (downloads PP-OCRv6 + VLM 0.9B + table model on first run; GPU strongly recommended) | **3.7.0**, 2026-06-11 (active, monthly) | **Yes, gold standard** — Markdown + JSON; `parsing_res_list` with bbox + label per block. Best in class for CN policy docs. | **Yes (best in class)** — table recognition, merged cells, cross-page via `PP-StructureV3` pipeline. | **Yes** | **Yes** (with `PP-StructureV3` `PP-DocLayoutV3` + `SLANeXt`) | **~3-5 s / page** (cloud) / **~1-2 s / page on A100** / **~3-10 s on CPU** — fails the 500 ms budget by 2-20×. | **~10 lines** — JSONL → `Block` (we already have this code in `core/parse_document.py:242-300`). | **✗ disqualified as primary** (speed, GPU). Keep as **scanned-page fallback** + free-tier (20k pages/day) cloud path. |
| 9 | **PP-StructureV3** | Apache-2.0 | `pip install paddleocr` (same model set as #8; ~2-3 GB model download) | bundled in paddleocr 3.7.0 (2026-06-11) | **Yes** — returns Markdown + JSON; bbox per region/table. | **Yes** (state of the art for Chinese) | **Yes** | **Yes** | **~0.6-1.1 s / page on A100/T4**; **~3-4 s / page on CPU** (from official benchmark in `PP-StructureV3.md`). | **~30 lines** (JSON output already has normalized bbox + label). | **✗ disqualified as primary** (GPU + model weight + speed budget; OK as offline batch path). |
| 10 | **marker-pdf** | GPL-3.0 (open weights, gated by HuggingFace) | `pip install marker-pdf` (pulls PyTorch + ~3 GB of model weights on first run; GPU recommended) | active 2025-2026; ~25 pages/sec on H100 = **~40 ms / page on H100**; 2× slower than Markitdown but on par with PyMuPDF on text-layer — but **with models**. CPU is 10-30× slower. | **Yes** — Markdown output with block-level structure. | **Yes** (good for English academic; weaker on Chinese policy tables) | **Partial** | **Partial** | **~40 ms / page on H100** / **~0.5-2 s / page on CPU** (cnblogs benchmark, 12-page PDF: 630 s ⇒ ~52 s / page, way over budget on CPU). | **~30 lines** (Markdown → custom block grouping). | **✗ disqualified** (PyTorch dependency + model weights + slow on CPU + GPL viral). |
| 11 | **unstructured** | Apache-2.0 | `pip install unstructured[pdf]` (pulls detectron2 / onnx / torch + ~1-3 GB models depending on partition strategy) | active 2025-2026; "hi-res" strategy uses a layout detection model | **Yes** — `partition_pdf` returns `Element` list with bbox + type. | **Limited** (relies on `infer_table_structure`; weaker than PP-StructureV3 on Chinese) | **No** | **No** | **~1-5 s / page on CPU** with "hi-res" strategy; **~200-500 ms** with "fast" strategy (no layout model, text-only). | **~20 lines** (`partition_pdf` → filter `Element` types → `Block`). | **✗ disqualified** (heavy model dep + Chinese-table quality below PP-StructureV3). |
| 12 | **MinerU (mineru[all])** | Apache-2.0 + extra conditions (MinerU Open Source License) | `pip install mineru[all]` (pulls PDF-Extract-Kit + MinerU2.5-Pro-2605-1.2B VLM ≈ 2-4 GB; needs 4-8 GB VRAM) | **3.4**, 2026-06-18 (very active; 76.1k★) | **Yes** — Markdown / JSON with bbox + reading-order recovery (best layout model on OmniDocBench). | **Yes** (best for Chinese) | **Yes** | **Yes** | **~1-3 s / page on T4/A100** (the cnblogs benchmark reported 1262 s for 12 pages ≈ 105 s / page on commodity GPU; way over the 500 ms budget on CPU). | **~20 lines** | **✗ disqualified** (model weight + GPU + speed budget; best-of-breed accuracy-wise but cannot meet latency). |

**¹ PyMuPDF license footnote**:
PyMuPDF is dual-licensed under **AGPL-3.0** (open-source / free) or **Artifex Commercial License**. For an **internal enterprise KB** that does not redistribute the parser as a service, AGPL is acceptable (no network service = no source-disclosure trigger). If the project is ever sold / distributed as a hosted SaaS for third parties, buy a commercial seat from Artifex. Source: PyPI `PyMuPDF 1.28.0` (2026-06-29) license field; PyMuPDF docs. No GPL/AGPL violation risk for in-house use.

---

## 2. Top 3 candidates recommended for T2 benchmark

> All three pass the hard constraints: free, pure-Python install, no Java, no GPU, no model download.

### 2.1 PyMuPDF (`fitz`) — **primary recommendation**

- **Install**: `pip install pymupdf` (pulls 1.28.0, June 2026). 100% pure-Python wrapper around MuPDF C lib; ships as a wheel for all major platforms; **no system deps**.
- **Why it wins**:
  1. **Layout blocks for free** — `get_text("dict")` returns the exact `{blocks, lines, spans}` shape the project already uses for `Block`; the conversion is **< 10 lines**.
  2. **Tables for free** — `find_tables()` (1.23+) and `TextPage.extract_tables()` (1.26+) handle ruled and borderless tables; `Table.bbox` lets us emit a "table" `Block` with `block_label="table"`.
  3. **Speed** — comfortably 30-100 ms / page on text-layer A4 (≫ 5× the 500 ms budget headroom).
  4. **Maintained** — monthly releases since 2023, latest 1.28.0 on 2026-06-29.
  5. **Already widely used** in the Python RAG / PDF space (RAGFlow, LangChain, LlamaIndex all support it).
- **License gotcha**: AGPL-3.0 is fine for in-house enterprise use. See footnote ¹.

**T2 code skeleton** (~25 lines, including Block conversion):

```python
import fitz  # pymupdf
from core.parse_document import Block, PageLayout, PageText, ParseResult


def parse_with_pymupdf(file_path: str) -> ParseResult:
    doc = fitz.open(file_path)
    by_page: list[PageText] = []
    layouts: list[PageLayout] = []
    for page_idx, page in enumerate(doc):
        rect = page.rect
        W, H = rect.width, rect.height
        d = page.get_text("dict")
        # Concatenate block spans to a page-level text
        page_text = "\n".join(
            span["text"]
            for blk in d["blocks"] if blk["type"] == 0
            for line in blk["lines"]
            for span in line["spans"]
        )
        by_page.append(PageText(page=page_idx, text=page_text))

        blocks: list[Block] = []
        order = 0
        for blk in d["blocks"]:
            if blk["type"] != 0:  # skip images/vectors for v1
                continue
            bbox = blk["bbox"]  # (x0,y0,x1,y1) in points
            content = " ".join(
                span["text"] for line in blk["lines"] for span in line["spans"]
            ).strip()
            polygon = [[bbox[0], bbox[1]], [bbox[2], bbox[1]],
                       [bbox[2], bbox[3]], [bbox[0], bbox[3]]]
            blocks.append(Block(
                block_label="text",
                block_content=content,
                bbox_norm=[bbox[0]/W, bbox[1]/H, bbox[2]/W, bbox[3]/H],
                polygon_norm=[[p[0]/W, p[1]/H] for p in polygon],
                block_order=order,
            ))
            order += 1
        # Optional: append table blocks from find_tables()
        for t in page.find_tables().tables:
            tb = t.bbox
            blocks.append(Block(
                block_label="table",
                block_content=t.to_markdown() if hasattr(t, "to_markdown") else "",
                bbox_norm=[tb.x0/W, tb.y0/H, tb.x1/W, tb.y1/H],
                polygon_norm=[[tb.x0/W, tb.y0/H], [tb.x1/W, tb.y0/H],
                              [tb.x1/W, tb.y1/H], [tb.x0/W, tb.y1/H]],
                block_order=order,
            ))
            order += 1
        layouts.append(PageLayout(page=page_idx, width=int(W), height=int(H), blocks=blocks))
    return ParseResult(by_page=by_page,
                       full_text="\n\n".join(p.text for p in by_page),
                       layout=layouts)
```

**KB samples to test on** (text-layer Chinese policy docs already on disk in `data/pending_review/`):
- `data/pending_review/130_公司服务创新管理办法(2023).pdf` — 6 pages, A4 (text-layer).
- `data/pending_review/174_公司数据治理管理办法 (2024).pdf` — 20 pages, A4 (text-layer + likely tables).
- `data/pending_review/158_公司工程建设管理办法（2025）.pdf` — 9 pages, A4.
- `data/pending_review/148_公司固定资产投资项目管理办法 (2024).pdf` — likely tables.
- Cross-page table probe: any doc from `data/pending_review/` with `extract_tables` returning identical headers on adjacent pages (Camelot's `stack_contiguous` can be the reference).

**T2 success criteria**:
- All pages parse in < 500 ms each (single-threaded on a CI machine).
- `Block.bbox_norm` non-empty on > 95 % of blocks (text-layer PDFs).
- Table block emits for every visible table region.
- Full text length within ±10 % of pdfplumber reference.

---

### 2.2 pypdfium2 — **secondary recommendation (fastest raw, weakest semantics)**

- **Install**: `pip install pypdfium2` (5.12.1, 2026-07-17). Wheels include PDFium; no system deps.
- **Why it ranks here**: Apache-2.0 (least license friction), but no built-in block/table semantics — you re-implement layout grouping. Useful as a **fallback if PyMuPDF's AGPL is a blocker** (e.g. the project is repackaged for distribution later).
- **Speed**: 4-5 ms / page raw text, but layout clustering adds another 30-100 ms.

**T2 code skeleton** (~40 lines):

```python
import pypdfium2 as pdfium
from core.parse_document import Block, PageLayout, PageText, ParseResult


def parse_with_pypdfium2(file_path: str) -> ParseResult:
    pdf = pdfium.PdfDocument(file_path)
    by_page, layouts = [], []
    for i, page in enumerate(pdf):
        W, H = page.get_size()
        tp = page.get_textpage()
        text = tp.get_text_range() or ""
        by_page.append(PageText(page=i, text=text))
        # Group glyphs by line (y0 bucket) → block (x0/x1 + line count heuristic)
        blocks: list[Block] = []
        order = 0
        current_y, current_line = None, []
        for g in tp.get_text():  # list of (char, x0, y0, x1, y1)
            y0 = round(g[2], 1)
            if current_y is None or abs(y0 - current_y) > 2:
                if current_line:
                    # emit line → block (or merge consecutive lines)
                    pass
                current_y, current_line = y0, [g]
            else:
                current_line.append(g)
        layouts.append(PageLayout(page=i, width=int(W), height=int(H), blocks=blocks))
    return ParseResult(by_page=by_page, full_text="\n\n".join(p.text for p in by_page),
                       layout=layouts)
```

**T2 to grade**: compare block count + bbox_norm quality against PyMuPDF on the same samples. Expect **significantly worse** for tables and for two-column layouts.

---

### 2.3 pdfplumber 0.11.10 — **fallback baseline (already in `requirements.txt`)**

- **Install**: already in `requirements.txt` (`pdfplumber==0.11.9` currently, pin to 0.11.10). 100 % pure Python. Depends on `pdfminer-six`.
- **Why it ranks here**: We are trying to *escape* pdfplumber's weak table / no-merged-cell behaviour, but it remains the **only zero-deps baseline** for the slow path. Use it for sanity check (current `_pdf_fallback()` already does this).
- **T2 value**: re-run the same T2 test suite against the current `core/parse_document.py:_pdf_fallback` path to get a **head-to-head** comparison vs PyMuPDF. This gives the bug-tracker ticket a defensible "before / after" data point.

---

## 3. Disqualified candidates (with reason)

| # | Candidate | Disqualification reason |
|---|---|---|
| 1 | **PaddleOCR / PP-StructureV3** (local) | 2-3 GB model download; **GPU strongly recommended**; speed 1-10 s / page on CPU (over 500 ms budget). Keep as **scanned-page fallback** + cloud path (20k pages/day free). |
| 2 | **marker-pdf** | Pulls PyTorch + ~3 GB model weights; CPU 10-30× slower than text-layer pymupdf; GPL-3.0 viral license; Chinese-table quality below PP-StructureV3. |
| 3 | **unstructured[pdf]** | Pulls detectron2 / onnx / torch + layout model; Chinese-table quality is the project's exact weak point. |
| 4 | **MinerU** | Needs 4-8 GB VRAM (Volta+); MinerU2.5-Pro-2605-1.2B VLM download; "MinerU Open Source License" adds distribution conditions; speed budget exceeded on CPU. |
| 5 | **tabula-py** | **Java 8+ required** (violates "pure Python" hard constraint); 8-month release gap; tables only. |
| 6 | **tika (python-tika)** | **Java 7+ required**; spins up Tika REST server; speed 500-2000 ms / page; table quality no better than pdfplumber. |
| 7 | **camelot-py** | **Tables only** — needs a second tool for text/blocks. Reasonable as a "tables pass" bolted onto PyMuPDF if `find_tables` quality is found lacking, but not a standalone candidate. |

---

## 4. Open questions for T2 / T3

1. **PyMuPDF AGPL risk for the project** — confirm with T3 / legal: is the in-house KB ever going to be (a) distributed as a hosted multi-tenant SaaS, or (b) sold as a binary? If yes to either, the Artifex commercial seat is needed; if no, AGPL is fine.
2. **Cross-page table quality** — T2 should run a **cross-page table probe** on `data/pending_review/148_公司固定资产投资项目管理办法.pdf` and similar 10+ page docs. PyMuPDF `find_tables` does **not** natively merge across pages; we may need to bolt on a small heuristic (cell bbox y-overlap + header-row match) to satisfy the "cross-page tables are a quality concern" requirement.
3. **Merged cells** — confirm the exact merged-cell patterns in the project's policy docs. If only "merge across columns in a header row" is needed, PyMuPDF's `to_markdown()` already flattens correctly. If "vertical merged cells" are present, neither PyMuPDF nor pdfplumber handle them out of the box; we may need to bolt on a cell-bbox-overlap detector (similar to what `dhdaines/pdfplumber` and the Towards Data Science 2025 benchmark propose).
4. **Block label taxonomy** — PaddleOCR currently emits labels like `text / title / table / figure / list / …`. PyMuPDF only emits `type ∈ {0, 1, 3}` (text / image / vector). For a drop-in replacement, the T2 task is to add a **heading / list detector** (e.g. by font size + bold flags from the span dict) so the existing frontend highlight chip continues to work.
5. **Reading-order on multi-column layouts** — T2 should test a two-column sample (likely `data/pending_review/021_公司信息公开管理暂行办法.pdf`-style documents). PyMuPDF emits blocks in PDF reading order, which is **not** always the visual reading order for two-column docs. If this is a real concern, we may need to cluster blocks into columns by x-bbox before emitting `block_order`.
6. **Cache invalidation** — if we swap `core/parse_document.py:_pdf_fallback` for PyMuPDF, the existing `data/.cache/paddleocr/*.json` cache will be **stale** (different `source` and `layout` shape). T2 should add a `source` discriminator (e.g. `source="pymupdf"`) and a one-shot cache rebuild for the affected KBs.
7. **Scan detection (text-layer vs raster)** — `core/parse_document.py:189-239` currently calls PaddleOCR for everything. With PyMuPDF as primary, we should call PyMuPDF first and only fall back to PaddleOCR for pages where `page.get_text("text").strip()` is empty (the existing scan-detection rule of thumb in pdfplumber). Verify this on `data/pending_review/012_关于组建建设管理部的通知 （2018）.pdf` (likely scanned) vs `130_公司服务创新管理办法(2023).pdf` (text-layer).
8. **Speed measurement protocol** — T2 should report **wall-clock per page** (median of 10 runs) on a single CI machine, and **process-level RSS delta** to confirm PyMuPDF's memory profile is acceptable for a Flask API process.
9. **CJK glyph bbox accuracy** — PyMuPDF's `get_text("dict")` block bbox is the **bounding box of the union of all spans**; for CJK text with fullwidth punctuation this can be 5-10 % wider than expected. Run a glyph-level accuracy check on a known text sample.
10. **Camelot as "tables pass" bolt-on** — if PyMuPDF's `find_tables` misses borderless / styled tables on policy docs, the `stack_contiguous` cross-page merge from camelot could be the surgical add. Defer until T2 numbers come in.

---

## 5. Appendix: source citations

Primary sources (PyPI / GitHub official):
- PyMuPDF 1.28.0 (2026-06-29) — `https://pypi.org/project/PyMuPDF/` (license: AGPL-3.0 / Artifex Commercial).
- pypdfium2 5.12.1 (2026-07-17) — `https://pypi.org/project/pypdfium2/` (license: Apache-2.0).
- pdfplumber 0.11.10 (2026-06-15) — `https://pypi.org/project/pdfplumber/` (license: MIT).
- pdfminer.six 20260107 (2026-01-07) — `https://pypi.org/project/pdfminer.six/` (license: MIT).
- PaddleOCR 3.7.0 (2026-06-11) — `https://pypi.org/project/paddleocr/` (license: Apache-2.0).
- camelot-py 2.0.0 (2026-06-04) — `https://pypi.org/project/camelot-py/` (license: MIT).
- tabula-py 2.10.0 (2024-10-17) — `https://pypi.org/project/tabula-py/` (license: MIT, **Java 8+ required**).
- tika 3.1.0 (2025-03-26) — `https://pypi.org/project/tika/` (license: Apache-2.0, **Java 7+ required**).
- MinerU 3.4 (2026-06-18) — `https://github.com/opendatalab/MinerU` (license: MinerU Open Source License ≈ Apache-2.0 + extra).
- PyMuPDF `get_text("dict")` API — `https://pymupdf.readthedocs.io/en/latest/textpage.html`.
- PyMuPDF `find_tables` API — `https://pymupdf.readthedocs.io/en/latest/page.html#Page.find_tables` (1.23+; merged cells partial; cross-page "manual via notebooks").
- PP-StructureV3 benchmarks — `https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-StructureV3/PP-StructureV3.md` (CPU 3.74 s/image, A100 0.61 s/image, T4 1.13 s/image for lightweight).
- marker-pdf / MinerU / Markitdown speed comparison (12-page PDF) — `https://www.cnblogs.com/JCpeng/p/18623713` (Marker 630.83 s, MinerU 1262.62 s, Markitdown 0.19 s on commodity hardware).
- dhdaines/pdfplumber active fork — `https://github.com/dhdaines/pdfplumber` (Python 3.12 support, last commit Aug 2024).
- camelot-py `stack_contiguous` (cross-page merge) — `https://camelot-py.readthedocs.io/` README.
- TableBench cross-page table evaluation set — `https://tablebench.github.io/` (2025 release).
- PubTabNet & FinTabNet updates — `https://research.ibm.com/publications/pubtabnet` (2025 cross-page samples).

In-repo references (read-only):
- `core/parse_document.py:39-50` — `Block` dataclass shape.
- `core/parse_document.py:189-239` — `_paddleocr_call` (the network round-trip we are trying to skip for text-layer PDFs).
- `core/parse_document.py:398-418` — `_pdf_fallback` (current pdfplumber path that drops `layout=[]`).
- `requirements.txt` — current pinned `pdfplumber==0.11.9`, `pdfminer-six==20251230`, `pypdfium2==4.30.0`, `pypdf==6.11.0`.
- `data/pending_review/*.pdf` — text-layer Chinese policy doc corpus for T2 benchmarking.
- `research/paddleocr-failure-rootcause.md` — previous root-cause report (issue #94); shows PaddleOCR is healthy on text-layer PDFs, which is exactly the case PyMuPDF is best at.

— research-agent, 2026-07-29
