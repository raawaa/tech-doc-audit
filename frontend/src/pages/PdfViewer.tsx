/**
 * PdfViewer — 生产 PDF 预览页(V9 PRD #68)。
 *
 * 基于 `@embedpdf/react-pdf-viewer` 的 drop-in `<PDFViewer>` 组件,挂在
 * `/pdf-viewer/:docId`。取代此前基于 headless plugins 的实现
 * (V9 PRD #62 → #63/#64/#65,#66 spike 并排验证 drop-in 后由 #68 正式切换)。
 * 演进史见 `docs/adr/0006-pdf-viewer-embedpdf-dropin.md`。
 *
 * 三条 URL 契约(auditor 点审核结果 chip 打开 `?block_range=…`):
 * - `?page=N` 自动跳页(scroll.onLayoutReady + scrollToPage)
 * - `?block_range=A,B` 坐标高亮(annotation plugin 的 importAnnotations)
 * - `?highlight=<text>` 字符串匹配 fallback(扫描全页)
 *
 * 设计要点:
 * - **坐标语义**:不走 matchBlockRangeToBlocks(它输出画布像素坐标)。
 *   直接读 `bbox_norm` × `layout.page.width/height` 把 0-1 框映射到 PDF
 *   用户空间(pt,左下原点)。
 * - **不调 commit()**:导入的 annotation 只活在内存视图里,刷新后消失,
 *   不污染源 PDF 字节。
 * - **工具栏精简**:drop-in 自带 toolbar,通过 `DISABLED_CATEGORIES` 砍掉
 *   annotation/redaction/form/print/export/history 等,保留 selection 让
 *   auditor 能复制 PDF 原文引用到审核报告,保留 zoom/navigation/scroll/page
 *   基础浏览能力。
 * - **outer header**:文档名 / 页码 / 跳页输入 / E1 重新解析 / 🔍高亮 /
 *   📍block_range 状态展示,与 drop-in viewer 内层共存。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import {
  PDFViewer,
  PdfAnnotationSubtype,
  type PluginRegistry,
  type ScrollCapability,
  type AnnotationCapability,
  type AnnotationTransferItem,
  type PageChangeEvent,
  type PdfHighlightAnnoObject,
} from '@embedpdf/react-pdf-viewer'
import type { LayoutReadyEvent } from '@embedpdf/plugin-scroll'
import { uuidV4 } from '@embedpdf/models'
import {
  blockMatchesHighlight,
  type Block as LayoutBlock,
} from '../lib/layoutMatch'
import { useScrollMode } from '../contexts/ScrollMode'

// ── URL 解析 helpers ──────────────────────────────────────────────────────

/** `block_range` URL 参数 `"start,end"` → `[start, end] | null`。 */
function parseBlockRangeParam(raw: string | null): [number, number] | null {
  if (!raw) return null
  const parts = raw.split(',')
  if (parts.length !== 2) return null
  const start = Number.parseInt(parts[0].trim(), 10)
  const end = Number.parseInt(parts[1].trim(), 10)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  if (start < 0 || end < start) return null
  return [start, end]
}

/** object-form setSearchParams 会 wipe,改用 functional form 保留其他键。 */
function updateSearchParam(
  setSearchParams: ReturnType<typeof useSearchParams>[1],
  key: string,
  value: string,
) {
  setSearchParams(prev => {
    const next = new URLSearchParams(prev)
    next.set(key, value)
    return next
  })
}

// ── layout / meta 类型 ────────────────────────────────────────────────────

interface LayoutPage {
  page: number
  width: number
  height: number
  blocks: LayoutBlock[]
}

interface LayoutDoc {
  layout: LayoutPage[]
  has_layout: boolean
}

interface LayoutState {
  data: LayoutDoc | null
  loading: boolean
  error: 'not-found' | 'other' | null
}

interface DocMeta {
  id: string
  name: string
  file_type: string
  page_count: number | null
}

const INITIAL_LAYOUT: LayoutState = { data: null, loading: false, error: null }

// ── 命中计算 + annotation 构造(不走 matchBlockRangeToBlocks)────────────

interface HitRectsByPage {
  hitsByPage: Map<number, LayoutBlock[]>
  firstHitPage0: number | null
}

/**
 * URL 三条路径汇成命中 map:
 * - block_range 命中:记下该页 blocks,firstHit = 命中页
 * - block_range 不命中 + highlight 有:fallback 全页扫描,firstHit = 最小命中页
 * - 都没:空 map
 *
 * 返回的 **block 引用**由 buildAnnotationsForLayout 重读 bbox_norm,与
 * matchBlockRangeToBlocks 的 "画布像素坐标" 输出解耦。
 */
function pickBlocksForHits(
  layout: LayoutDoc | null,
  blockRange: [number, number] | null,
  highlight: string,
  urlPage: number,
): HitRectsByPage {
  const hitsByPage = new Map<number, LayoutBlock[]>()
  let firstHitPage0: number | null = null
  if (!layout) return { hitsByPage, firstHitPage0 }

  const tryBlockRange = (): boolean => {
    if (!blockRange) return false
    const targetPage0 = urlPage - 1
    const page = layout.layout.find(p => p.page === targetPage0)
    if (!page) return false
    const matching = page.blocks.filter(b => {
      const order = b.block_order ?? -1
      return order >= blockRange[0] && order <= blockRange[1]
    })
    if (matching.length === 0) return false
    hitsByPage.set(page.page, matching)
    firstHitPage0 = page.page
    return true
  }

  const scanAllPagesByText = () => {
    // 复用 lib/layoutMatch.blockMatchesHighlight 的 T1+P2 判定,不在本文件
    // 再复制一份 N3+includes+LCS。空 highlight 由 predicate 短路。
    for (const page of layout.layout) {
      const matching = page.blocks.filter(b => blockMatchesHighlight(b, highlight))
      if (matching.length > 0) {
        hitsByPage.set(page.page, matching)
        if (firstHitPage0 === null || page.page < firstHitPage0) {
          firstHitPage0 = page.page
        }
      }
    }
  }

  if (tryBlockRange()) return { hitsByPage, firstHitPage0 }
  if (highlight) scanAllPagesByText()
  return { hitsByPage, firstHitPage0 }
}

/**
 * 把命中 + page dims 一起过,产出最终 annotation list。
 *
 * 坐标转换契约(bbox_norm × page.width/height,PDF 用户空间):
 * - 输入 bbox_norm:  `[x1, y1, x2, y2]` 0-1 归一化,**顶原点**(CSS 风格)
 * - 输入 page.width/height: 来自 layout API 的物理页尺寸,等于 PDF pt
 * - 输出 rect origin: `(x1 * w, pageH - y2 * pageH)` 左下原点,Y-up
 * - 输出 rect size:  `((x2-x1) * w, (y2-y1) * pageH)`
 *
 * 每 block 一个独立 PdfHighlightAnnoObject,共享 annotation author 给审计日志,
 * color/opacity 走硬编码值便于视觉一致。
 */
function buildAnnotationsForLayout(
  layout: LayoutDoc,
  hitsByPage: Map<number, LayoutBlock[]>,
): AnnotationTransferItem[] {
  const items: AnnotationTransferItem[] = []
  for (const [page0, blocks] of hitsByPage) {
    const layoutPage = layout.layout.find(p => p.page === page0)
    if (!layoutPage) continue
    const pageW = Math.max(layoutPage.width, 1)
    const pageH = Math.max(layoutPage.height, 1)
    for (const block of blocks) {
      const bbox = block.bbox_norm
      if (!Array.isArray(bbox) || bbox.length !== 4) continue
      const [x1n, y1n, x2n, y2n] = bbox
      const w = (x2n - x1n) * pageW
      const h = (y2n - y1n) * pageH
      if (w <= 0 || h <= 0) continue
      // embedpdf Highlight:CSS 顶原点,scale prop 把 rect 当 PDF pt 转 CSS px。
      // 不要 Y-flip。DPR 校正不在这里做(embedpdf 还没 ready,拿不到 scale),
      // 在 import 时从 scroll.getMetrics() 读 effectiveDPR 校正。
      const rect = {
        origin: { x: x1n * pageW, y: y1n * pageH },
        size: { width: w, height: h },
      }
      const anno: PdfHighlightAnnoObject = {
        type: PdfAnnotationSubtype.HIGHLIGHT,
        id: uuidV4(),
        pageIndex: page0,
        rect,
        segmentRects: [rect],
        opacity: 0.4,
        strokeColor: '#FFFF00',
        author: 'audit-system',
      }
      items.push({ annotation: anno })
    }
  }
  return items
}

// ── drop-in 配置(模块级常量,避免每 render 重建)─────────────────────

/**
 * 砍掉 drop-in 自带 UI 的白名单反义。
 *
 * 设计取舍:
 * - 不列 'selection-copy'/'selection' 系列,selection 默认开,auditor 能复制
 *   PDF 原文。
 * - 不列 'zoom','navigation','scroll','page'(保留基础浏览能力)。
 * - snippet 内部容错忽略未知 category,冗余项不影响运行。
 */
const DISABLED_CATEGORIES: string[] = [
  'annotation', 'annotation-markup', 'annotation-highlight',
  'annotation-underline', 'annotation-strikeout', 'annotation-squiggly',
  'redaction', 'redaction-area', 'redaction-text', 'redaction-apply', 'redaction-clear',
  'form', 'form-textfield', 'form-checkbox', 'form-radio', 'form-select', 'form-listbox',
  'insert', 'insert-rubber-stamp', 'insert-signature', 'insert-image',
  'document-print', 'document-capture', 'document-export', 'document-fullscreen',
  'panel-sidebar', 'panel-search', 'panel-comment',
  'pan', 'pointer',
  'history', 'history-undo', 'history-redo',
  'thumbnail', 'bookmark', 'attachment',
  'capture', 'stamp', 'signature',
  'redact-mode',
]

// ── 顶层 PdfViewer:状态机 + 装配 drop-in viewer ──────────────────────

/**
 * 从 embedpdf 读 effectiveDPR = renderScale / cssScale,用来校正 annotation rect。
 * embedpdf Highlight 的 `scale` prop 是渲染 scale(含 DPR),不是 CSS scale。
 * 直接传 PDF-pt rect 会被多乘 DPR → 高亮位置/尺寸翻倍。
 * 必须在 import 时从 scroll.getMetrics() 拿(构造时 viewer 还没 ready)。
 *
 * DPR 是 embedpdf 全局渲染参数,与"哪页在 viewport"无关(PRD #72 — 早期
 * 实现只查 page 1 的 metrics,非首页命中时 cssScale 拿 page 1 width 算,
 * 比例错 → effectiveDPR 估成 1,annotation rect 不被除 DPR,X/Y/尺寸翻倍)。
 * 修:用 pageVisibilityMetrics 第一个可见页的 metric 推 effectiveDPR,
 * pdfPageW 按该可见页的 pageNumber 反查 layout 拿(width 会随页变)。
 */
function getEffectiveDpr(
  scroll: ScrollCapability | null,
  layout: LayoutDoc | null,
): number {
  if (!scroll) return 1
  try {
    const metrics = scroll.getMetrics()
    const pm = metrics.pageVisibilityMetrics[0]
    if (!pm || pm.scaled.visibleWidth <= 0) return 1
    const pdfPageW =
      layout?.layout.find(p => p.page === pm.pageNumber - 1)?.width ?? 1
    const cssScale = pm.scaled.visibleWidth / pdfPageW
    if (cssScale <= 0) return 1
    return pm.scaled.scale / cssScale
  } catch {
    return 1
  }
}

/** 把 raw rect(以 effectiveDpr 校正)应用到 AnnotationTransferItem[] */
function applyEffectiveDpr(
  items: AnnotationTransferItem[],
  dpr: number,
): AnnotationTransferItem[] {
  if (dpr === 1) return items
  return items.map(item => {
    const a = item.annotation
    const scaleRect = (r: { origin: { x: number; y: number }; size: { width: number; height: number } }) => ({
      origin: { x: r.origin.x / dpr, y: r.origin.y / dpr },
      size: { width: r.size.width / dpr, height: r.size.height / dpr },
    })
    return {
      ...item,
      annotation: {
        ...a,
        rect: scaleRect(a.rect),
        // segmentRects 只在 PdfHighlightAnnoObject 上存在;类型守卫后再 map
        ...(('segmentRects' in a && a.segmentRects)
          ? { segmentRects: (a.segmentRects as Array<typeof a.rect>).map(scaleRect) }
          : {}),
      },
    }
  })
}

export function PdfViewer() {
  const { docId: docIdParam } = useParams<{ docId: string }>()
  const docId = docIdParam || ''
  const [searchParams, setSearchParams] = useSearchParams()
  const targetPage = parseInt(searchParams.get('page') || '1', 10)
  const highlight = searchParams.get('highlight') || ''
  const blockRange = parseBlockRangeParam(searchParams.get('block_range'))

  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [layout, setLayout] = useState<LayoutState>(INITIAL_LAYOUT)
  const [reparsing, setReparsing] = useState(false)
  const [textContent, setTextContent] = useState('')
  const [textTotalPages, setTextTotalPages] = useState(0)

  // ── scroll 模式驱动 ─────────────────────────────────────
  // 把 `meta.file_type` 反映到外层 <main> overflow:
  //   'pdf' → 'hidden'  (embedpdf viewer 内嵌滚,占满 viewport 高度)
  //   其他  → 'default' (外层 overflow-y-auto 行为不变)
  // meta 加载前保持 default(loading spinner 自身短,不必特殊处理)。
  // 卸载时 reset 避免残留。
  const { setMode: setScrollMode } = useScrollMode()
  useEffect(() => {
    if (!meta) {
      setScrollMode('default')
      return
    }
    setScrollMode(meta.file_type === 'pdf' ? 'hidden' : 'default')
  }, [meta?.file_type, setScrollMode])
  useEffect(() => () => setScrollMode('default'), [setScrollMode])

  // Drop-in viewer 给的实时状态 — 通过 onPageChange 订阅
  const [currentPage, setCurrentPage] = useState(targetPage)
  const [totalPages, setTotalPages] = useState(0)
  const [viewerStatus, setViewerStatus] = useState<
    'init' | 'loading' | 'ready' | 'error'
  >('init')

  // 一次性 latch:每个文档只 import 一次 annotation,只 scrollToPage 一次
  const importedRef = useRef(false)
  const jumpedRef = useRef(false)
  const registryRef = useRef<PluginRegistry | null>(null)
  // 镜像 annotationsToImport,让 onLayoutReady 闭包能读到最新值。
  // 闭包在 handleReady 订阅时形成;若 layout API 那时还没回,闭包里是空
  // 数组,importAnnotations 跑空 → 高亮全无(2026-07-09 #68 修复)。
  const annotationsRef = useRef<AnnotationTransferItem[]>([])

  const apiBase = import.meta.env.VITE_API_BASE_URL || ''
  const pdfUrl =
    meta?.file_type === 'pdf'
      ? `${apiBase}/api/v1/kb-documents/${docId}/file`
      : null

  // ── 计算 hits(URL → layout 命中)──────────────────────────────
  const { hitsByPage, firstHitPage0 } = useMemo(
    () => pickBlocksForHits(layout.data, blockRange, highlight, targetPage),
    [layout.data, blockRange, highlight, targetPage],
  )
  const annotationsToImport = useMemo(
    () => layout.data
      ? buildAnnotationsForLayout(layout.data, hitsByPage)
      : [],
    [layout.data, hitsByPage],
  )
  // 同步最新 annotation list 到 ref,供 onLayoutReady 闭包读取
  annotationsRef.current = annotationsToImport

  // ── 文档 meta ──────────────────────────────────────────────
  useEffect(() => {
    if (!docId) return
    const ctrl = new AbortController()
    fetch(`${apiBase}/api/v1/kb-documents/${docId}`, { signal: ctrl.signal })
      .then(r => { if (!r.ok) throw new Error('文档不存在'); return r.json() })
      .then((m: DocMeta) => setMeta(m))
      .catch(e => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e.message)
      })
      .finally(() => setLoading(false))
    return () => ctrl.abort()
  }, [docId, apiBase])

  // ── layout ──────────────────────────────────────────────────
  useEffect(() => {
    if (!meta || meta.file_type !== 'pdf') return
    const ctrl = new AbortController()
    setLayout({ data: null, loading: true, error: null })
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/layout`, { signal: ctrl.signal })
      .then(r => {
        if (r.status === 404) {
          setLayout({ data: null, loading: false, error: 'not-found' })
          return null
        }
        if (!r.ok) throw new Error(`layout fetch ${r.status}`)
        return r.json() as Promise<LayoutDoc>
      })
      .then((doc: LayoutDoc | null) => {
        if (!doc) return
        setLayout({ data: doc, loading: false, error: null })
      })
      .catch(e => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setLayout({ data: null, loading: false, error: 'other' })
      })
    return () => ctrl.abort()
  }, [meta, docId, apiBase])

  // ── DOCX/MD 文本降级 ──────────────────────────────────────────
  useEffect(() => {
    if (!meta || meta.file_type === 'pdf') return
    const ctrl = new AbortController()
    const page = Math.max(targetPage - 1, 0)
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/page/${page}`, { signal: ctrl.signal })
      .then(r => {
        if (!r.ok) throw new Error(`text page fetch ${r.status}`)
        return r.json()
      })
      .then((d: { text: string; total_pages: number }) => {
        setTextContent(d.text)
        setTextTotalPages(d.total_pages)
      })
      .catch(e => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e.message)
      })
    return () => ctrl.abort()
  }, [meta, docId, targetPage, apiBase])

  // ── drop-in viewer onReady:接 registry,自动跳页 + 灌 annotation ──
  const handleReady = useCallback((registry: PluginRegistry) => {
    if (registryRef.current === registry) return
    registryRef.current = registry
    setViewerStatus('ready')

    // 仅 dev / E2E 暴露 registry 句柄(playwright 跑 `npm run dev`)。
    // 生产 build `import.meta.env.DEV === false`,不挂 window,避免泄露。
    if (import.meta.env.DEV && typeof window !== 'undefined') {
      ;(window as unknown as { __pdfViewerRegistry?: Promise<PluginRegistry> })
        .__pdfViewerRegistry = Promise.resolve(registry)
    }

    const scrollPlugin = registry.getPlugin('scroll')
    if (!scrollPlugin) return
    // IPlugin.provides is optional in @embedpdf/core; runtime invariant: registered
    // plugins always have it. The cast pins this for the duration of handleReady.
    const scroll = (scrollPlugin.provides?.() as ScrollCapability | undefined) ?? null
    if (!scroll) return

    // 订阅 page-change — 头部 page-counter 实时更新
    scroll.onPageChange((evt: PageChangeEvent) => {
      setCurrentPage(evt.pageNumber + 1)
      setTotalPages(evt.totalPages)
    })

    // 等 layout ready 后:auto-jump + importAnnotations(各只一次)
    scroll.onLayoutReady((evt: LayoutReadyEvent) => {
      if (evt.documentId !== docId) return
      const target = firstHitPage0 !== null ? firstHitPage0 + 1 : targetPage

      if (!jumpedRef.current) {
        jumpedRef.current = true
        try {
          scroll.forDocument(docId).scrollToPage({
            pageNumber: target,
            behavior: 'auto',
          })
        } catch (e) {
          console.warn('[PdfViewer] scrollToPage failed', e)
        }
      }

      // 从 ref 读最新值,避免订阅时闭包捕获空数组的竞态
      const toImport = annotationsRef.current
      if (!importedRef.current && toImport.length > 0) {
        importedRef.current = true
        const annPlugin = registry.getPlugin('annotation')
        if (!annPlugin) {
          console.warn('[PdfViewer] annotation plugin missing')
          return
        }
        // IPlugin.provides is optional in @embedpdf/core; runtime invariant always set.
        const ann = (annPlugin.provides?.() as AnnotationCapability | undefined) ?? null
        if (!ann) {
          console.warn('[PdfViewer] annotation plugin missing')
          return
        }
        try {
          // DPR 校正:embedpdf scale prop 是 renderScale = cssScale × effectiveDPR,
          // 我们传的是 PDF-pt rect,得除 effectiveDPR 才落到 CSS px。
          const dpr = getEffectiveDpr(scroll, layout.data)
          const corrected = applyEffectiveDpr(toImport, dpr)
          ann.forDocument(docId).importAnnotations(corrected)
          // importAnnotations 的 annotation 默认 commitState='new',默认不渲染;
          // 显式 commit() 把它们转到 'synced' 才会画上屏。autoCommit:true 只对
          // CREATE_ANNOTATION 路径生效,import 路径不走那条 reducer(2026-07-09 修)。
          ann.forDocument(docId).commit()
        } catch (e) {
          console.warn('[PdfViewer] importAnnotations failed', e)
        }
      }
    })
  }, [docId, targetPage, firstHitPage0])

  // ── header 跳页:直接调 scroll capability 再同步 URL ───────────
  //   (URL 的 page 变化不重跑 onLayoutReady latch,导航必须走 scrollToPage)
  const handleHeaderJump = useCallback((n: number) => {
    if (!Number.isFinite(n) || n < 1) return
    const registry = registryRef.current
    const scroll = registry
      ? ((registry.getPlugin('scroll')?.provides?.() as ScrollCapability | undefined) ?? null)
      : null
    try {
      scroll?.forDocument(docId).scrollToPage({ pageNumber: n, behavior: 'auto' })
    } catch (e) {
      console.warn('[PdfViewer] header scrollToPage failed', e)
    }
    updateSearchParam(setSearchParams, 'page', String(n))
  }, [docId, setSearchParams])

  // ── 兜底 import:onLayoutReady 先 fire(layout API 还没回)时它跑空,这里
  //   监 annotationsToImport 变非空,触发一次 import。每文档仅一次(latch)。
  useEffect(() => {
    if (importedRef.current) return
    if (annotationsToImport.length === 0) return
    const registry = registryRef.current
    if (!registry) return  // 引擎还没 ready,onLayoutReady 那边会处理
    // 等 annotation state 初始化:onLayoutReady 在 annotation state 创建之后
    // 才 fire,所以这里等到 viewerStatus === 'ready' 再试。
    if (viewerStatus !== 'ready') return
    try {
      const ann = (registry.getPlugin('annotation')?.provides?.() as AnnotationCapability | undefined) ?? null
      if (!ann) return
      // 等 annotation state 存在(importAnnotations 内部会触发其创建)
      const list = annotationsToImport
      importedRef.current = true
      // DPR 校正(同 onLayoutReady 路径)
      const scroll = (registry.getPlugin('scroll')?.provides?.() as ScrollCapability | undefined) ?? null
      const dpr = getEffectiveDpr(scroll, layout.data)
      const corrected = applyEffectiveDpr(list, dpr)
      ann.forDocument(docId).importAnnotations(corrected)
      // 显式 commit() 让 import 路径的 annotation 也能渲染
      ann.forDocument(docId).commit()
    } catch (e) {
      console.warn('[PdfViewer] fallback importAnnotations failed', e)
      importedRef.current = false  // 失败时解锁,允许重试
    }
  }, [annotationsToImport, viewerStatus, docId])

  // ── E1 重新解析按钮 ────────────────────────────────────────
  const handleReparse = useCallback(async () => {
    setReparsing(true)
    try {
      const r = await fetch(
        `${apiBase}/api/v1/kb-documents/${docId}/reparse`,
        { method: 'POST' },
      )
      if (!r.ok) {
        setError(`重新解析失败: ${r.status}`)
        return
      }
      setError('已提交重新解析,请稍后刷新页面查看 layout')
    } finally {
      setReparsing(false)
    }
  }, [apiBase, docId])

  // ── 顶层状态机 ───────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
      </div>
    )
  }
  if (error && !meta) {
    return <div className="text-center py-20 text-red-500">{error}</div>
  }
  if (!meta) {
    return <div className="text-center py-20 text-slate-500">文档不存在</div>
  }

  const showE1 = layout.error === 'not-found' && (!!highlight || !!blockRange)
  const showE2 = layout.error === 'other' && (!!highlight || !!blockRange)

  // drop-in disabledCategories — 模块级常量 DISABLED_CATEGORIES,
  // 避免每次 render 重建数组引用。selection/zoom/navigation 默认开。
  // `documentManager.initialDocuments[].documentId` 让 embedpdf 用 URL docId
  // 标识文档,否则 onLayoutReady 会拿到自动生成的 `doc-<ts>-<rand>` 跟我们的
  // docId 不一致,importAnnotations / scrollToPage 全部走 early-return 跳过。
  // 顶层 `documentId` 字段不存在(只有 `src` / `worker` / `wasmUrl` 等);
  // document id 走 documentManager 子配置(原 headless 版本也是这条路)。
  const dropinConfig = pdfUrl
    ? {
        src: pdfUrl,
        worker: false as const,
        // pdfium.wasm: 沿用公共目录(若缺则 drop-in 用内置路径)
        wasmUrl: '/pdfium.wasm',
        tabBar: 'never' as const,
        disabledCategories: DISABLED_CATEGORIES,
        annotations: { annotationAuthor: 'audit-system' },
        documentManager: {
          initialDocuments: [{ url: pdfUrl, documentId: docId }],
        },
      }
    : { tabBar: 'never' as const, worker: false as const }

  return (
    // PDF 模式:由外层 <main>(ScrollMode=hidden)提供高度,根容器 h-full flex-col 占满,
    // PDF 子容器继续 flex-1 拿掉 sticky header 后的剩余高度。
    // text-fallback 模式:保留 min-h-screen 维持原 "外层页面 scroll" 行为(#71 brief)。
    <div
      data-testid="pdf-viewer"
      className={
        meta.file_type === 'pdf'
          ? 'h-full bg-slate-100 flex flex-col'
          : 'min-h-screen bg-slate-100'
      }
    >
      {/* 强制 embedpdf annotation 画层在 page canvas 之上。
         embedpdf 默认 annotation zIndex=0,跟 page canvas 同级时按 DOM 顺序,
         annotation 先渲染 → 视觉上被 page 盖住。点击触发 zIndex:1 才浮到上面。
         这里用 CSS 把 annotation 容器直接拉到 zIndex:3,常驻可见。 */}
      <style>{`
        [data-testid="pdf-viewer-container"] [data-embedpdf-managed="true"] > * {
          position: relative;
        }
        [data-testid="pdf-viewer-container"] [data-embedpdf-managed="true"] > div:last-child {
          z-index: 3;
        }
        [data-testid="pdf-viewer-container"] [data-embedpdf-managed="true"] canvas {
          z-index: 1;
        }
      `}</style>
      {/* Header */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">
            {meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页
          </p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {meta.file_type === 'pdf' && (
            <HeaderStatus
              currentPage={currentPage}
              totalPages={totalPages}
              targetPage={targetPage}
              pageCount={meta.page_count}
              onJump={handleHeaderJump}
            />
          )}
          {viewerStatus === 'init' && meta.file_type === 'pdf' && (
            <span className="text-xs text-slate-400">PDF 加载中…</span>
          )}
          {viewerStatus === 'error' && (
            <span className="text-xs text-red-500">PDF viewer 错误</span>
          )}
          {meta.file_type !== 'pdf' && textTotalPages > 0 && (
            <TextNav
              targetPage={targetPage}
              textTotalPages={textTotalPages}
              onJump={n => updateSearchParam(setSearchParams, 'page', String(n))}
            />
          )}
          {highlight && (
            <span className="text-xs text-amber-600 ml-2">
              🔍 高亮: {highlight.slice(0, 50)}
            </span>
          )}
          {blockRange && (
            <span className="text-xs text-amber-600 ml-2">
              📍 block_range: {blockRange.join(',')}
            </span>
          )}
          {showE1 && (
            <div className="flex items-center gap-2 ml-2 text-xs text-slate-400">
              <span>该文档未解析,无法定位引用位置</span>
              <button
                className="px-2 py-1 rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
                onClick={handleReparse}
                disabled={reparsing}
              >
                {reparsing ? '提交中…' : '重新解析'}
              </button>
            </div>
          )}
          {showE2 && (
            <div className="flex items-center gap-2 ml-2 text-xs text-slate-400">
              <span>无法读取 layout 数据</span>
            </div>
          )}
        </div>
      </div>

      {/* Content */}
      {meta.file_type === 'pdf' ? (
        <div
          data-testid="pdf-viewer-container"
          className="relative flex-1 min-h-0"
        >
          <PDFViewer
            config={dropinConfig}
            onReady={handleReady}
            className="absolute inset-0"
          />
        </div>
      ) : (
        <div className="flex justify-center py-6" data-testid="text-fallback">
          <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
            <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {textContent || '（该页无文本内容）'}
            </pre>
            {textTotalPages > 0 && (
              <p className="text-xs text-slate-400 mt-4">
                第 {targetPage} / {textTotalPages} 页
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── header 子组件:viewer 实时状态(由 onPageChange 推送)+ 跳页输入 ────────

function HeaderStatus(props: {
  currentPage: number
  totalPages: number
  targetPage: number
  pageCount: number | null
  onJump: (page: number) => void
}) {
  const { currentPage, totalPages, targetPage, pageCount, onJump } = props
  const isKnown = totalPages > 0
  const maxPage = totalPages || pageCount || undefined
  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span data-testid="page-counter">
        {isKnown ? `${currentPage} / ${totalPages}` : `${targetPage} / ${pageCount || '?'}`}
      </span>
      <button
        className="px-2 py-1 rounded border border-slate-300 hover:bg-slate-50"
        onClick={() => onJump(Math.max(targetPage - 1, 1))}
        data-testid="header-prev-page"
      >←</button>
      <input
        type="number"
        className="w-14 px-2 py-1 border rounded text-center text-xs"
        min={1}
        max={maxPage}
        defaultValue={String(targetPage)}
        onKeyDown={e => {
          if (e.key === 'Enter') {
            const n = parseInt((e.target as HTMLInputElement).value, 10)
            onJump(n)
          }
        }}
        data-testid="page-jump-input"
      />
      <button
        className="px-2 py-1 rounded border border-slate-300 hover:bg-slate-50"
        onClick={() => onJump(targetPage + 1)}
        data-testid="header-next-page"
      >→</button>
    </div>
  )
}

function TextNav(props: {
  targetPage: number
  textTotalPages: number
  onJump: (page: number) => void
}) {
  const { targetPage, textTotalPages, onJump } = props
  return (
    <>
      <button
        className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
        disabled={targetPage <= 1}
        onClick={() => onJump(targetPage - 1)}
      >←</button>
      <span className="text-slate-600 tabular-nums text-sm">{targetPage} / {textTotalPages}</span>
      <button
        className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
        disabled={targetPage >= textTotalPages}
        onClick={() => onJump(targetPage + 1)}
      >→</button>
    </>
  )
}
