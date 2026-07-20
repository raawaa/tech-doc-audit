# PdfViewer URL Contract & Restore Specification

> Spec for issue #74. **Violating this contract is a bug; silent tolerance is forbidden.**
> Reference implementation: `frontend/src/pages/PdfViewer.tsx`. Cross-references:
> ADR-0006 (pitfalls), `CONTEXT.md` (ubiquitous language + coordinate semantics),
> `frontend/e2e/pdf-viewer.spec.ts` (behavioral assertions).

## 1. Three URL contracts (URL is the input)

The viewer accepts exactly three query parameters. Adding a fourth is forbidden
without a new spec amendment.

### 1.1 `?page=N` — 1-indexed target page

- Parsed as `parseInt(searchParams.get('page') || '1', 10)`.
  Source: `frontend/src/pages/PdfViewer.tsx:305`.
- Semantics: **1-indexed**, aligned with the header `page-counter` text
  ("`currentPage / totalPages`"), which is also 1-indexed
  (`frontend/src/pages/PdfViewer.tsx:452`, where `evt.pageNumber + 1` is fed
  to `setCurrentPage`).
- Use case: when no `block_range` / `highlight` is given, this is the only
  navigation hint; `firstHitPage0 === null` so `target = targetPage`
  (`frontend/src/pages/PdfViewer.tsx:459`).
- Out of contract: `?page=` value ≤ 0 or non-numeric — `parseInt` returns `NaN`;
  the resulting `target = NaN` propagates into
  `scrollToPage({pageNumber: NaN, ...})` (`frontend/src/pages/PdfViewer.tsx:464`)
  and embedpdf ignores it. No silent fallback to page 1. Behavior is undefined
  in this corner; tests must not rely on it.

### 1.2 `?block_range=A,B` — coordinate highlight

- Parser: `parseBlockRangeParam` in `frontend/src/pages/PdfViewer.tsx:51-60`.
- Contract:
  - `0 ≤ start ≤ end` (both integers).
  - `start < 0 || end < start` → returns `null`, treated as "not provided".
  - Non-numeric / wrong arity / empty string → `null`.
- Source values are **block_order** (KB layout block ordinals), not page
  numbers. See `CONTEXT.md` §"block_range" for the chunk→block_range mapping
  (`start_block_order` = first matching layout block in page; `end_block_order`
  = last).
- Lookup is scoped to the **single page indicated by `?page=`** (not all pages):
  `layout.layout.find(p => p.page === targetPage0)` at
  `frontend/src/pages/PdfViewer.tsx:133`. `targetPage0 = urlPage - 1` (line 132).
- **Priority**: `block_range` is tried first in `pickBlocksForHits`
  (`frontend/src/pages/PdfViewer.tsx:159`). If it yields ≥ 1 hit,
  `?highlight=` is **never consulted** — even if both are present in the URL.
- Hits: every matching block yields exactly one `PdfHighlightAnnoObject`
  (`frontend/src/pages/PdfViewer.tsx:200-210`). One block → one annotation.

### 1.3 `?highlight=<text>` — fallback string scan

- Parsed as `searchParams.get('highlight') || ''`
  (`frontend/src/pages/PdfViewer.tsx:306`). Empty string and `null` are
  equivalent: both treated as "not provided".
- When provided (non-empty): only used if `block_range` did not produce any
  hit, or `block_range` was absent. See priority order in `pickBlocksForHits`
  (`frontend/src/pages/PdfViewer.tsx:159-161`).
- Predicate: `blockMatchesHighlight(block, highlight)` from
  `frontend/src/lib/layoutMatch.ts:264-283` — T1 includes (NFKC normalized) +
  P2 LCS fallback (ratio ≥ 0.85, `min(len) >= 4`). Reusing this predicate
  prevents semantic drift vs. the legacy string-match path.
- `firstHit` for auto-jump = the smallest 0-based page index that contains a
  match (`frontend/src/pages/PdfViewer.tsx:152-154`).

## 2. Coordinate semantics

PDF user space, bottom-left origin, Y-up. **No Y-flip.**

### 2.1 Construction (`buildAnnotationsForLayout`)

Source: `frontend/src/pages/PdfViewer.tsx:176-214`.

- Inputs:
  - `bbox_norm: [x1, y1, x2, y2]` — **top-origin** (CSS-style), 0-1 normalized,
    from `/layout` API block.
  - `page.width` / `page.height` — physical PDF page dimensions in **PDF pt**
    (returned by the layout API, treated as equal to PDF pt).
- Output `rect`:
  - `origin.x = x1 * pageW`
  - `origin.y = y1 * pageH`  ← **NOT** `pageH - y2 * pageH`. Y-up.
  - `size.width = (x2 - x1) * pageW`
  - `size.height = (y2 - y1) * pageH`
- Edge handling: `w <= 0 || h <= 0` skips the block (line 192). `bbox_norm`
  must be length 4; otherwise skip (line 188).

### 2.2 DPR pre-division (the "every PDF-pt rect must be pre-divided" rule)

- embedpdf's `scale` prop on Highlight is `renderScale = cssScale × effectiveDPR`.
  Passing a raw PDF-pt rect multiplies it by DPR (≈ 2× on retina). See
  ADR-0006 pitfall #5 (`docs/adr/0006-pdf-viewer-embedpdf-dropin.md:43`).
- Every rect produced by §2.1 must be divided by `effectiveDPR` before import.
  Implementation: `applyEffectiveDpr` in `frontend/src/pages/PdfViewer.tsx:276-299`,
  applied at import time at lines 491-492 (`onLayoutReady` path) and 539-540
  (fallback path).
- DPR=1 short-circuit: `applyEffectiveDpr` returns items unchanged when
  `dpr === 1` (line 280).

## 3. Restore timing (mount → first commit)

Sequence in `PdfViewer.tsx`, anchored to line numbers:

1. **Mount** — `frontend/src/pages/PdfViewer.tsx:301-348` (component init, refs
   declared: `importedRef`, `jumpedRef`, `registryRef`, `annotationsRef`).
2. **Fetch meta** — effect at `frontend/src/pages/PdfViewer.tsx:370-382`
   (`/api/v1/kb-documents/${docId}`). On success → `setMeta` →
   `setLoading(false)`.
3. **Fetch layout** (parallel-ish) — effect at
   `frontend/src/pages/PdfViewer.tsx:385-407` (`/api/v1/kb-documents/${docId}/layout`).
   On 404 → `error: 'not-found'`; non-OK non-404 → `error: 'other'`.
4. **embedpdf ready** — `<PDFViewer>` calls `onReady` →
   `handleReady` (`frontend/src/pages/PdfViewer.tsx:431-503`). Sets
   `viewerStatus='ready'`, exposes registry on `window.__pdfViewerRegistry`
   in DEV (line 438-441), grabs `scroll` capability.
5. **`onLayoutReady`** — subscribed inside `handleReady` at
   `frontend/src/pages/PdfViewer.tsx:457`. **Early-return** if
   `evt.documentId !== docId` (line 458) — see §6.
6. **Compute `firstHit`** — `pickBlocksForHits` over `(layout.data, blockRange,
   highlight, targetPage)`. Memoized at `frontend/src/pages/PdfViewer.tsx:356-359`.
7. **`scrollToPage`** — once per document (`jumpedRef` latch at line 461-471).
   Target = `firstHitPage0 + 1` if a hit was found, else `targetPage`
   (line 459).
8. **`importAnnotations`** — once per document (`importedRef` latch at line 475).
   Reads latest list from `annotationsRef.current` (line 474) to dodge the
   closure-capture race (ADR-0006 pitfall #2).
9. **`commit()`** — `ann.forDocument(docId).commit()` at line 497. Required per
   ADR-0006 pitfall #3 — the Highlight paint path is gated on
   `'dirty'` / `'synced'` states, and `'new'` is the default for imported
   annotations. `commit()` is the sanctioned transition. If a post-commit
   annotation reports `commitState === 'new'` (round-trip assertion at
   `frontend/e2e/pdf-viewer.spec.ts:535`), that is a bug under investigation
   in #75/#76, not a working state.

### 3.1 Latch invariants

- `importedRef.current` must become `true` at most once per mounted document.
- `jumpedRef.current` must become `true` at most once per mounted document.
- On import failure, `importedRef` is reset to `false` (line 546) to allow
  retry; this is the only sanctioned deviation from "once per document".

## 4. DPR contract

`getEffectiveDpr(scroll, layout)` — `frontend/src/pages/PdfViewer.tsx:256-273`.

- Formula: `pm.scaled.scale / (pm.scaled.visibleWidth / pdfPageW)`, where:
  - `pm = metrics.pageVisibilityMetrics[0]` — first **visible** page's metric.
  - `pdfPageW` = `layout.layout.find(p => p.page === pm.pageNumber - 1)?.width`.
    Note: page is 1-based in embedpdf, 0-based in layout — hence `pm.pageNumber - 1`.
- Guard rails (any of which ⇒ return 1):
  - `scroll === null` (engine not ready).
  - `pageVisibilityMetrics[0]` absent.
  - `pm.scaled.visibleWidth <= 0`.
  - `pdfPageW` not found in layout (also returns 1 via `?? 1`).
  - `cssScale <= 0`.
  - Any thrown error from `scroll.getMetrics()` (try/catch at line 261-272).
- **Must NOT fall back to `window.devicePixelRatio`.** See
  `CONTEXT.md` §"annotation rect 坐标" `Avoid` note: physical DPR and
  effectiveDPR diverge (browser zoom, etc.). This is the #72 fix
  (inherited by #74) — using page 1's metric when the user is on page N
  was a real bug.
- Source page for `pdfPageW` is the **first visible page's** layout record,
  not page 1. This is the explicit fix vs. the pre-#72 implementation
  (lines 250-255 commentary).

## 5. Fallback path

The two-step restore admits a race: `onLayoutReady` can fire before
`/layout` returns. Both paths must `commit()`.

### 5.1 Primary path (`onLayoutReady` in `handleReady`)

- `frontend/src/pages/PdfViewer.tsx:457-502`. Imports inside the
  `onLayoutReady` callback, reads `annotationsRef.current`, guards on
  `importedRef` + `toImport.length > 0`. Closes with `commit()`.

### 5.2 Fallback path (`useEffect` over `annotationsToImport`)

- `frontend/src/pages/PdfViewer.tsx:523-548`.
- Triggers when: `!importedRef.current && annotationsToImport.length > 0
  && registryRef.current && viewerStatus === 'ready'`.
- Skips when: layout API hasn't returned yet (no registry yet → returns
  line 527), or viewer not ready (line 530), or already imported (line 524).
- Calls `applyEffectiveDpr(...)` and `ann.forDocument(docId).commit()`
  exactly like the primary path (lines 538-543).
- Failure path unlocks `importedRef` (line 546) so the next `annotationsToImport`
  change can retry.

### 5.3 Why both must `commit()`

`importAnnotations` produces annotations in `commitState: 'new'`. The Highlight
component's paint path renders only `'dirty'` / `'synced'` states.
`commit()` is the only sanctioned transition. See ADR-0006 pitfall #3. If a
post-commit annotation reports `commitState === 'new'` (round-trip assertion
at `frontend/e2e/pdf-viewer.spec.ts:535`), that is a bug under investigation
in #75/#76, not a working state — the spec does not retroactively bless it.

## 6. docId anchoring

`frontend/src/pages/PdfViewer.tsx:588-604`.

- `PDFViewerConfig` does **not** have a top-level `documentId` field.
- The drop-in must be told our URL docId via
  `documentManager.initialDocuments[].documentId === docId` (line 603).
- If this is wrong, embedpdf auto-generates `doc-<ts>-<rand>`. Then:
  - `onLayoutReady`'s `evt.documentId !== docId` ⇒ early-return at line 458.
  - Every `forDocument(docId)` call inside `handleReady` (lines 464, 493, 497)
    hits a different document than the one the registry is observing.
  - **Every restore path is dead.** No scroll, no import, no commit. This is
    ADR-0006 pitfall #1 — non-negotiable.
- Verifying channel for tests: `waitForViewerRegistry` poll on
  `window.__pdfViewerRegistry` in `frontend/e2e/pdf-viewer.spec.ts:39-50`.

## 7. Error states

Layout fetch returns one of three outcomes; UI reacts
(`frontend/src/pages/PdfViewer.tsx:583-584`, rendering 676-692):

| Error                 | Source line     | UI                                              |
|-----------------------|-----------------|-------------------------------------------------|
| `layout.error === 'not-found'` (HTTP 404) AND `highlight || blockRange` | 583, 676-687 | **E1**: "该文档未解析" + "重新解析" button → POST `/api/v1/kb-documents/${docId}/reparse` (lines 551-566) |
| `layout.error === 'other'` AND `highlight || blockRange` | 584, 688-692 | **E2**: "无法读取 layout 数据" (no button) |
| `layout.error` set, neither `highlight` nor `blockRange` | 583, 584 | Nothing — silent (just no highlights). |

- "重新解析" button is disabled while `reparsing` is true (line 682).
- Failure of the reparse POST sets `error` (line 559) and surfaces a message
  "已提交重新解析,请稍后刷新页面查看 layout" (line 562) — this is informational,
  not E1/E2.
- E1/E2 are gated on `highlight || blockRange`: a viewer opened with only
  `?page=N` does not show "无法定位引用位置" when layout is missing, because
  there is nothing to locate.

## 8. ADR-0006 pitfalls (non-negotiable)

These five pitfalls are locked by ADR-0006
(`docs/adr/0006-pdf-viewer-embedpdf-dropin.md:37-43`). They are **not**
whitewashable into "normal behavior" by future tickets. Any fix that
silently tolerates a violation of these — e.g. by patching one symptom
while leaving the root cause — is a bug.

1. **`documentId` must be set via `documentManager.initialDocuments[].documentId`.**
   Top-level `documentId` does not exist in `PDFViewerConfig`. Without
   `initialDocuments[].documentId === URL docId`, embedpdf uses
   `doc-<ts>-<rand>` and every `onLayoutReady` / `forDocument(docId)` path
   is dead. See §6.
2. **`onLayoutReady` closure must not capture stale `annotationsToImport`.**
   `handleReady` subscribes once; the closure captures the value of
   `annotationsToImport` at subscription time. Use `annotationsRef` to mirror
   the latest value (line 347, 367). Pair with the §5 fallback effect.
3. **`importAnnotations` must be followed by an explicit `commit()`.**
   `commitState: 'new'` is the default for imported annotations; the
   Highlight paint path renders only `'dirty'` / `'synced'`. The
   `autoCommit: true` snippet option only applies to the
   `CREATE_ANNOTATION` reducer, not the `import` path. See §5.3.
4. **Annotation z-index must be manually raised above page canvas.**
   The drop-in's `<style>` block at `frontend/src/pages/PdfViewer.tsx:624-634`
   forces `[data-embedpdf-managed="true"] > div:last-child` to
   `z-index: 3` so the highlight layer is not buried under the page canvas.
   Removing this CSS re-introduces the "highlight behind page" bug.
5. **effectiveDPR must be derived from `scroll.getMetrics()`, not
   `window.devicePixelRatio`.** Physical DPR and effectiveDPR can differ
   (browser zoom etc.). See §4 and CONTEXT.md `Avoid` note.

## 9. How to cite in e2e

`frontend/e2e/pdf-viewer.spec.ts` is the canonical behavioral assertion
surface. Mapping spec sections → test files:

| Spec section                                | E2E test(s)                                                                                                                                |
|---------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| §1.1 `?page=N`                              | "header page-jump-input + Enter" (line 542); "?page=1&highlight=<非首页条款名>" auto-jump (line 367)                                        |
| §1.2 `?block_range=A,B`                     | "合法 doc + block_range 参数" (line 266); "block_range=3,3 单 block" (line 430); "block_range=2,5 多 block" (line 448)                     |
| §1.3 `?highlight=` fallback                 | "合法 doc + highlight 字符串参数" (line 283); "block_range … 命中无 + highlight 命中" fallback (line 470); "旧 ?highlight=" (line 491)       |
| §2 coordinate semantics                    | "round-trip" (line 510): asserts `rectOriginY ≈ 1600` (Y-up), `rectOriginX ≈ 50`, `rectWidth ≈ 900`, `rectHeight ≈ 200`                    |
| §3 restore timing                           | All "waitForViewerRegistry" + `expect.poll(getAnnotationCount)` patterns                                                                 |
| §4 DPR contract                             | "?page=N(N≥2)&block_range=…" (line 411) — the #72 regression assertion                                                                  |
| §5 fallback path                            | Implicit: the "?page=1&highlight=" tests cover both layout-returned-before and layout-returned-after race                                   |
| §6 docId anchoring                          | Implicit in `waitForViewerRegistry` + any `getAnnotationCount` test that resolves to a positive count                                    |
| §7 E1 / E2                                  | "未 reparse doc + 带 highlight:E1 fallback UI 出现" (line 298)                                                                            |
| §8 pitfalls                                 | Each pitfall is regression-locked by at least one named test above; failure to reproduce the original bug ⇒ pitfall regression              |

### 9.1 New spec assertions must

- Use `waitForViewerRegistry(page)` before reading annotations.
- Use `expect.poll(() => getAnnotationCount(...))` rather than fixed sleeps.
- Read state through `window.__pdfViewerRegistry`, not via DOM scraping
  (the drop-in renders into a managed DOM that does not expose
  `data-testid="highlight-rect"` anymore — CONTEXT.md §"PDF viewer architecture"
  `Avoid`).
- For new assertions on rect, prefer `getFirstAnnotationPayload`
  (`frontend/e2e/pdf-viewer.spec.ts:104`), which reads `rect.origin` /
  `rect.size` from the JS annotation state. (`getRectPositionForPage` is
  referenced in `CONTEXT.md` §"E2E annotation 断言钩子" as the
  visual-pixel-position hook but is not yet defined in the codebase; treat
  it as aspirational.)

## 10. Out of scope for this spec

- Re-debating any of the §8 pitfalls. They are referenced, not reopened.
- Adding new URL parameters. The three in §1 are locked.
- Implementing code changes. This document is a contract; behavior change
  requires a new ticket that amends this spec.
- `worker: true` restore. ADR-0006 §"取舍" notes this is closed; drop-in
  keeps `worker: false` (line 596).