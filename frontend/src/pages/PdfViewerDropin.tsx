/**
 * PdfViewerDropin — PRD #66 spike.
 *
 * `@embedpdf/react-pdf-viewer` 的 drop-in 包装,在
 * ``/pdf-viewer-dropin/:docId`` 路由上并排验证三条 URL 路径:
 *
 * - ``?page=N`` 自动跳页(scroll.onLayoutReady + scrollToPage)
 * - ``?block_range=A,B`` 坐标高亮(annotation plugin 的 importAnnotations)
 * - ``?highlight=<text>`` 字符串匹配 fallback(同前,扫描全页)
 *
 * 设计要点:
 * - **坐标语义**:不走 matchBlockRangeToBlocks(它输出画布像素坐标)。
 *   直接读 ``bbox_norm`` × ``layout.page.width/height`` 把 0-1 框映射到 PDF
 *   用户空间(pt,左下原点)。这是 PRD #66 验证的第 1 项坐标风险点。
 * - **不调 commit()**:导入的 annotation 只活在内存视图里。issue acceptance 的
 *   "刷新后高亮消失" 由此保证。
 * - **工具栏精简**:drop-in 自带大量 toolbar,通过 ``disabledCategories`` 砍掉
 *   annotation/redaction/form/print/export/history 等,保留 selection 让
 *   auditor 能复制 PDF 原文引用到审核报告。保留 zoom 类别(spec 明确要求
 *   "保留 zoom / navigation / scroll / page 等基础浏览能力")。
 * - **outer wrapper 保留现有 PdfViewer 的 header**:
 *   文档名 / 页码 / E1 重新解析按钮 / 🔍高亮 / 📍block_range 状态展示,
 *   与 drop-in viewer 内层共存,不替换生产 PdfViewer.tsx。
 * - **side-by-side**:不删生产 PdfViewer,不删 headless plugins,不删 layoutMatch.ts。
 *   spike 通过 → 转 PRD 走替换流程;不通过 → 关闭 issue。
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

// ── URL 解析 helpers(与 PdfViewer.tsx 同型)───────────────────────────────

/** ``block_range`` URL 参数 ``"start,end"`` → ``[start, end] | null``。 */
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

// ── layout / meta 类型(与 PdfViewer.tsx 同型)────────────────────────────

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
 * 与 PdfViewer.tsx:computeHits 同型语义:
 * - block_range 命中:记下该页 blocks,firstHit = 命中页
 * - block_range 不命中 + highlight 有:fallback 全页扫描,firstHit = 最小命中页
 * - 都没:空 map
 *
 * 返回的 **block 引用**由 buildAnnotationsPdf 重读 bbox_norm,与
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
    // 复用 lib/layoutMatch.blockMatchesHighlight 的 T1+P2 判定,不在 spike
    // 端再复制一份 N3+includes+LCS。同时空 highlight 由 predicate 短路。
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
 * - 输入 bbox_norm:  ``[x1, y1, x2, y2]`` 0-1 归一化,**顶原点**(CSS 风格)
 * - 输入 page.width/height: 来自 layout API 的物理页尺寸,等于 PDF pt
 * - 输出 rect origin: ``(x1 * w, pageH - y2 * pageH)`` 左下原点,Y-up
 * - 输出 rect size:  ``((x2-x1) * w, (y2-y1) * pageH)``
 *
 * 每 block 一个独立 PdfHighlightAnnoObject,共享 annotation author 给审计日志,
 * color/opacity 走 PRD 要求的硬编码值便于视觉对比 headless 路径截图。
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
      // PDF user space: 左下角原点,Y-up
      const rect = {
        origin: { x: x1n * pageW, y: pageH - y2n * pageH },
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
 * - 不列 'zoom','navigation','scroll','page'(spec 要求保留"基础浏览能力")。
 * - 'document-open'/'document-close'/'document-protect' 在 spec 列表里但
 *   snippet 文档未列出这些 category 名。snipper 内部容错忽略未知 category,
 *   不影响 spike。
 */
const SPIKE_DISABLED_CATEGORIES: string[] = [
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

// ── 顶层 PdfViewerDropin:状态机 + 装配 drop-in viewer ──────────────────

export function PdfViewerDropin() {
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

  // ── DOCX/MD 文本降级(沿用 PdfViewer 的 fallback,与 spike 无直接关系)──────
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

    // 暴露给 E2E 测试(spike 自带,生产 PdfViewer 没有此 handle)
    if (typeof window !== 'undefined') {
      ;(window as unknown as { __pdfViewerDropinRegistry?: Promise<PluginRegistry> })
        .__pdfViewerDropinRegistry = Promise.resolve(registry)
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
          console.warn('[PdfViewerDropin] scrollToPage failed', e)
        }
      }

      if (!importedRef.current && annotationsToImport.length > 0) {
        importedRef.current = true
        const annPlugin = registry.getPlugin('annotation')
        if (!annPlugin) {
          console.warn('[PdfViewerDropin] annotation plugin missing')
          return
        }
        // IPlugin.provides is optional in @embedpdf/core; runtime invariant always set.
        const ann = (annPlugin.provides?.() as AnnotationCapability | undefined) ?? null
        if (!ann) {
          console.warn('[PdfViewerDropin] annotation plugin missing')
          return
        }
        try {
          ann.forDocument(docId).importAnnotations(annotationsToImport)
        } catch (e) {
          console.warn('[PdfViewerDropin] importAnnotations failed', e)
        }
      }
    })
  }, [docId, targetPage, firstHitPage0, annotationsToImport])

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

  // drop-in disabledCategories — 模块级常量 SPIKE_DISABLED_CATEGORIES,
  // 避免每次 render 重建数组引用。同时 selection/zoom/navigation 默认开。
  const dropinConfig = pdfUrl
    ? {
        src: pdfUrl,
        worker: false as const,
        // pdfium.wasm: 沿用公共目录(若缺则 drop-in 用内置路径)
        wasmUrl: '/pdfium.wasm',
        tabBar: 'never' as const,
        disabledCategories: SPIKE_DISABLED_CATEGORIES,
        annotations: { annotationAuthor: 'audit-system' },
      }
    : { tabBar: 'never' as const, worker: false as const }

  return (
    <div data-testid="pdf-viewer-dropin" className="min-h-screen bg-slate-100">
      {/* Header — 与生产 PdfViewer 同结构(spike 仅做并排对比) */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">
            {meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页
            <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-[10px]">
              dropin spike
            </span>
          </p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <DropinHeaderStatus
            currentPage={currentPage}
            totalPages={totalPages}
            targetPage={targetPage}
            pageCount={meta.page_count}
            onJump={n => updateSearchParam(setSearchParams, 'page', String(n))}
          />
          {viewerStatus === 'init' && (
            <span className="text-xs text-slate-400">drop-in viewer 加载中…</span>
          )}
          {viewerStatus === 'error' && (
            <span className="text-xs text-red-500">drop-in viewer 错误</span>
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
          data-testid="dropin-container"
          style={{ height: 'calc(100vh - 57px)' }}
          className="relative"
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

// ── header 子组件:dropin viewer 实时状态(由 onPageChange 推送)────────

function DropinHeaderStatus(props: {
  currentPage: number
  totalPages: number
  targetPage: number
  pageCount: number | null
  onJump: (page: number) => void
}) {
  const { currentPage, totalPages, targetPage, pageCount, onJump } = props
  const isKnown = totalPages > 0
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
      <button
        className="px-2 py-1 rounded border border-slate-300 hover:bg-slate-50"
        onClick={() => onJump(targetPage + 1)}
        data-testid="header-next-page"
      >→</button>
    </div>
  )
}
