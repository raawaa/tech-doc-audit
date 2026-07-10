# PDF viewer 采用 embedpdf drop-in（弃用 headless plugins）

生产 `/pdf-viewer/:docId` 经历了三次迭代，本 ADR 记录终态与被弃用路径，避免后人把已删除的 headless 实现当"丢失的功能"找回来。

## 演进

1. **起点（PRD #62）** — headless plugins 重写。react-pdf + react-virtuoso 的滚动条抖动（thumb 回弹、track 长度跳变，见 #62）促使切到 `@embedpdf/*` 的 headless plugins（DocumentManager + Viewport + Scroll + Render），高亮用页面 wrapper 上叠百分比 `<div data-testid="highlight-rect">`。分三个 slice #63/#64/#65 落地。
2. **spike（PRD #66）** — #62 的 PRD 曾明确拒绝 `@embedpdf/react-pdf-viewer` drop-in 方案（理由：drop-in 不暴露 `renderPage` slot，看不出怎么灌 URL 高亮）。#66 重开这个方向，做**并排 spike**（`/pdf-viewer-dropin/:docId`），验证用 annotation plugin 的 `importAnnotations(PdfHighlightAnnoObject[])` 把预构造高亮灌进 drop-in 内置 AnnotationLayer。spike 通过：坐标语义、color/opacity、commitState、Y-flip 方向都被 round-trip e2e 证实。
3. **替换（PRD #68，本 ADR）** — spike 通过后，正式把生产 `/pdf-viewer` 从 headless 切到 drop-in，删除 headless 实现与其专用 deps。

## 决策

生产 PDF viewer = `@embedpdf/react-pdf-viewer` 的 `<PDFViewer>` drop-in。理由：

- **UI parity 几乎白送**。drop-in 自带工具栏（zoom / navigation / page / selection），覆盖 #48"缺缩放/浏览模式"的原始诉求，还带来 headless 版没有的**文本选择**（auditor 可复制 PDF 原文进审核报告）。headless 版这些都得手写。
- **高亮改走 annotation**。不再是 headless 的百分比 div overlay，而是 annotation plugin 的 highlight annotation。坐标是 PDF 用户空间（pt，左下原点，Y-up），由 `bbox_norm × page.width/height` 转换；**不调 `commit()`**，annotation 只活在内存，刷新即消失，不污染源 PDF 字节。
- **滚动条稳定性保持**。#62 的核心 bug（react-virtuoso 重测量抖动）在 drop-in 上不复现——embedpdf 一次 layout commit。e2e 在 drop-in 上重新验证。

## 被弃用 / 删除的东西（别找回来）

- `frontend/src/pages/PdfViewer.tsx` 的 **headless 实现**（`EmbedPDF` + `usePdfiumEngine` + 四个 plugin package + `RenderLayer` + 百分比 `highlight-rect` div）——已被同名文件的 drop-in 实现覆盖。
- `frontend/src/pages/PdfViewerDropin.tsx` 与 `/pdf-viewer-dropin` 路由——spike 脚手架，已并入生产。
- deps `@embedpdf/core` / `engines` / `plugin-document-manager` / `plugin-render` / `plugin-viewport`——仅 headless 用，已从 `package.json` 移除（`plugin-scroll` 保留作 type，`models` 保留作 `uuidV4`，`react-pdf-viewer` 是主依赖）。
- `matchBlockRangeToBlocks`（`lib/layoutMatch.ts`）——headless 的坐标高亮入口，现**无 caller**（死函数）。保留文件是因为 `blockMatchesHighlight` / `matchHighlightToBlocks` 仍被 drop-in 的 `?highlight=` fallback 用。若后续确认不需要可单独清理。

## 取舍

- **体积**。`react-pdf-viewer` 把所有 plugin 打进 bundle（spike 实测单 index chunk +272 kB gzip）。本 PRD 用 `React.lazy` 把 viewer 路由拆成独立 chunk，主 `index-*.js` 不再含 embedpdf；复测走 `npm run bundle:report`。这是"接受更大的 viewer chunk 换 UI parity + 维护成本"的取舍。
- **worker: false**。沿用 headless 时代结论（#65 验证 Vite dev 与 preview 下 `worker: true` 都卡死，inline-blob-worker + pdfium.wasm 跨 worker fetch 失败）。drop-in 继续 `worker: false`，随 headless 的 worker follow-up 一起在 #62 关闭时作废——drop-in 路径不复用那条 worker 代码。

## 撤销条件

若 drop-in 的体积（即便拆 chunk）或工具栏定制受限（例如需要深度改注释编辑 UI）成为硬约束，可回到 headless plugins 自绘——但需重新承担 zoom / navigation / selection / 滚动稳定性的手写成本。删除的 headless 代码在 git 历史（`4d277ba` 之前）可取回。

## 实现踩坑（2026-07-09 真实调试产物）

5 个非显而易见的坑，每个都让高亮"不画 / 画错位 / 位置/尺寸翻倍":

1. **`documentId` 不在 `PDFViewerConfig` 顶层**——必须走 `documentManager.initialDocuments[].documentId`，否则 embedpdf 自动生成 `doc-<ts>-<rand>` 临时 id，`onLayoutReady` 的 `evt.documentId` 跟 URL docId 永远不等，所有 import 路径被 early-return 跳过。
2. **`onLayoutReady` 闭包竞态**——`handleReady` 订阅时形成闭包，捕获当时的 `annotationsToImport`（layout API 还没回时是 `[]`）。修法：`annotationsRef` 镜像、`onLayoutReady` 内读 ref 拿最新值，配合一个 `useEffect` 兜底 import。
3. **`commitState: 'new'` 的 annotation 不渲染**——`importAnnotations()` 灌进去的 annotation 默认 `new` 状态，Highlight 组件 paint 路径只画 `dirty` / `synced`。snippet 的 `autoCommit: true` 只对 `CREATE_ANNOTATION` reducer 生效，import 路径不走那条。修法：`importAnnotations(...)` 之后立刻 `commit()`。
4. **annotation 默认 `zIndex: 0`，被 page canvas 盖住**——需 CSS 把 `[data-embedpdf-managed="true"] > div:last-child` 拉到 `zIndex: 3` 常驻可见。
5. **embedpdf `scale` prop 是 `renderScale = cssScale × effectiveDPR`**，不是 `cssScale`——传 PDF-pt rect 会被多乘 DPR（实测 2x）。修法：从 `scroll.getMetrics().pageVisibilityMetrics[].scaled.scale / (visibleWidth / pdfPageW)` 读 `effectiveDPR`，import 时除 rect 校正。**不能用 `window.devicePixelRatio`**——物理 DPR 与 effectiveDPR 不一定相等（含 browser zoom 等倍率）。

完整坐标契约（**顶原点，不 Y-flip**，预除 effectiveDPR）见 `CONTEXT.md` 的 "annotation rect 坐标" 段。