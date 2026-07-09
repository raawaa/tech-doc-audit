/**
 * PdfViewer: 用 @embedpdf/* headless plugins 渲染 PDF,
 * URL 契约 `?page=` / `?block_range=` / `?highlight=` 全部接入。
 *
 * Slice 1 (#63): react-pdf + react-virtuoso → embedpdf DocumentManager +
 * Viewport + Scroll + Render。高亮机制由 canvas fillRect 改为页面 wrapper
 * 上叠 percentage `<div data-testid="highlight-rect">`。
 *
 * Slice 2 (#64): URL → 自动跳页 / 坐标高亮 / 文本匹配 fallback 三条路径
 * 全部接通。`useScrollCapability().onLayoutReady` 是"layout 准备好可跳"的
 * 信号,比之前的 `lastScrollSignatureRef` latch 稳定。
 *
 * 已知约束 (Slice 3 已验证):
 * - `usePdfiumEngine({ worker: false })` 避开 Vite dev 下 inline-blob-worker 卡死
 * - 2026-07-09 #65 验证 production build (vite preview) worker: true 同样卡死,
 *   见 issue #62 父评论。Follow-up:embedpdf 2.15+ 或 vendored worker
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { createPluginRegistration } from '@embedpdf/core'
import { EmbedPDF } from '@embedpdf/core/react'
import { usePdfiumEngine } from '@embedpdf/engines/react'
import { Viewport, ViewportPluginPackage } from '@embedpdf/plugin-viewport/react'
import {
  Scroller,
  ScrollPluginPackage,
  ScrollStrategy,
  useScroll,
  useScrollCapability,
} from '@embedpdf/plugin-scroll/react'
import {
  DocumentContent,
  DocumentManagerPluginPackage,
} from '@embedpdf/plugin-document-manager/react'
import { RenderLayer, RenderPluginPackage } from '@embedpdf/plugin-render/react'
import {
  matchBlockRangeToBlocks,
  matchHighlightToBlocks,
  type Block as LayoutBlock,
  type HighlightRect,
} from '../lib/layoutMatch'

// ── URL 解析 helpers ──────────────────────────────────────────────────────

/** ``block_range`` URL 参数 ``"start,end"`` → ``[start, end] | null``。
 *  V8-S6:AuditResult → PDF viewer 的标准链接 ``?block_range=start,end&page=N``
 *  经此处解析;失败 / 未命中 → fallback 到 `matchHighlightToBlocks` 文本匹配。
 */
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

/** 更新 search params 时保留其他键(block_range / highlight 等)——
 *  object-form setSearchParams 会 wipe 整张表,会破坏 V8 URL 契约。
 *  改用 functional form,把目标键以外的全部 copy 过去。
 */
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

// ── 命中计算：layout + URL 三条路径汇成一份 "命中 per 0-based page" ─────

interface HitsResult {
  hitsByPage: Map<number, HighlightRect[]>
  firstHitPage0: number | null
  pagePdfDims: Map<number, { w: number; h: number }>
}

/** 把 URL 三条路径 (block_range / highlight fallback / 都无) 汇成 hits map。
 *  - block_range 命中(>0 hits):记下 hits,firstHit = 命中页 0-based
 *  - block_range 不命中(0 hits)+ highlight 存在:fallback 到 highlight 全页扫描
 *  - highlight 命中:记下所有命中页的 rects,firstHit = 最小命中页 0-based
 *  - 都无:空 map
 *
 *  pagePdfDims 永远收集所有出现过的页(供 PageView 算 percentage 用)。
 */
function computeHits(
  layout: LayoutDoc | null,
  blockRange: [number, number] | null,
  highlight: string,
  urlPage: number,
): HitsResult {
  const hitsByPage = new Map<number, HighlightRect[]>()
  const pagePdfDims = new Map<number, { w: number; h: number }>()
  let firstHitPage0: number | null = null
  if (!layout) return { hitsByPage, firstHitPage0, pagePdfDims }

  const tryBlockRange = (): boolean => {
    if (!blockRange) return false
    const targetPage0 = urlPage - 1
    const page = layout.layout.find(p => p.page === targetPage0)
    if (!page) return false
    const pageW = Math.max(page.width, 1)
    const pageH = Math.max(page.height, 1)
    pagePdfDims.set(page.page, { w: pageW, h: pageH })
    const hits = matchBlockRangeToBlocks(
      blockRange, page.blocks, pageW, pageH, page.page,
    )
    if (hits.length === 0) return false
    hitsByPage.set(page.page, hits)
    firstHitPage0 = page.page
    return true
  }

  const scanAllPagesByText = () => {
    for (const page of layout.layout) {
      const pageW = Math.max(page.width, 1)
      const pageH = Math.max(page.height, 1)
      pagePdfDims.set(page.page, { w: pageW, h: pageH })
      const hits = matchHighlightToBlocks(
        highlight, page.blocks, pageW, pageH, page.page,
      )
      if (hits.length > 0) {
        hitsByPage.set(page.page, hits)
        if (firstHitPage0 === null || page.page < firstHitPage0) {
          firstHitPage0 = page.page
        }
      }
    }
  }

  if (tryBlockRange()) return { hitsByPage, firstHitPage0, pagePdfDims }
  if (highlight) scanAllPagesByText()
  return { hitsByPage, firstHitPage0, pagePdfDims }
}

// ── 单页渲染：embedpdf RenderLayer + 命中 rects overlay ─────────────────

function PageView(props: {
  documentId: string
  pageIndex: number
  width: number
  height: number
  hits: HighlightRect[]
  pagePdfWidth: number
  pagePdfHeight: number
}) {
  const { documentId, pageIndex, width, height, hits, pagePdfWidth, pagePdfHeight } = props
  return (
    <div
      data-testid="pdf-page"
      data-page-index={pageIndex}
      style={{ width, height, position: 'relative' }}
    >
      <RenderLayer documentId={documentId} pageIndex={pageIndex} />
      {hits.length > 0 && (
        <div
          className="absolute inset-0 pointer-events-none"
          data-testid="highlight-overlay"
        >
          {hits.map((h, i) => {
            const leftPct = (h.x / pagePdfWidth) * 100
            const topPct = (h.y / pagePdfHeight) * 100
            const wPct = (h.w / pagePdfWidth) * 100
            const hPct = (h.h / pagePdfHeight) * 100
            return (
              <div
                key={i}
                data-testid="highlight-rect"
                style={{
                  position: 'absolute',
                  left: `${leftPct}%`,
                  top: `${topPct}%`,
                  width: `${wPct}%`,
                  height: `${hPct}%`,
                  background: 'rgba(255, 255, 0, 0.4)',
                  border: '1px solid rgba(255, 200, 0, 0.7)',
                }}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── embedpdf 视图（拿到 engine 后才能挂载）───────────────────────────────

function EmbedContent(props: {
  documentId: string
  layout: LayoutDoc | null
  blockRange: [number, number] | null
  highlight: string
  urlPage: number
  onJump: (page: number) => void
}) {
  const { documentId, layout, blockRange, highlight, urlPage, onJump } = props
  const { provides: cap } = useScrollCapability()
  const { state } = useScroll(documentId)
  const { hitsByPage, firstHitPage0, pagePdfDims } = useMemo(
    () => computeHits(layout, blockRange, highlight, urlPage),
    [layout, blockRange, highlight, urlPage],
  )

  // 自动跳页 — 等 embedpdf onLayoutReady 触发后再 scrollToPage。
  // ref latch + urlPage 入参保证每文档仅跳一次:
  //   - 首次 mount 跳到 firstHitPage0(若有)/ urlPage
  //   - 后续 urlPage 变化(layout refresh)不重跳——用户 header 跳页走 cap.scrollToPage 直接调
  const jumpedRef = useRef(false)
  useEffect(() => {
    if (!cap) return
    if (jumpedRef.current) return
    const target = firstHitPage0 !== null ? firstHitPage0 + 1 : urlPage
    const off = cap.onLayoutReady((evt) => {
      if (jumpedRef.current) return
      if (evt.documentId !== documentId) return
      jumpedRef.current = true
      off()
      cap.scrollToPage({ pageNumber: target, behavior: 'auto' })
    })
    return () => {
      if (!jumpedRef.current) off()
    }
  }, [cap, documentId, firstHitPage0, urlPage])

  const handleHeaderJump = useCallback((n: number) => {
    if (!cap) return
    cap.scrollToPage({ pageNumber: n, behavior: 'auto' })
    onJump(n)
  }, [cap, onJump])

  return (
    <Viewport documentId={documentId} className="bg-slate-100">
      <div className="absolute top-2 right-2 z-10 bg-white border border-slate-200 rounded shadow px-3 py-1 text-xs flex items-center gap-2">
        <span data-testid="page-counter">
          {state.currentPage} / {state.totalPages} 页
        </span>
        <span>跳至</span>
        <input
          type="number"
          className="w-14 px-2 py-1 border rounded text-center text-xs"
          min={1}
          max={state.totalPages || undefined}
          defaultValue={String(urlPage)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              const n = parseInt((e.target as HTMLInputElement).value, 10)
              if (!Number.isFinite(n) || n < 1) return
              if (state.totalPages && n > state.totalPages) return
              handleHeaderJump(n)
            }
          }}
          data-testid="page-jump-input"
        />
        <span>页</span>
      </div>
      <Scroller
        documentId={documentId}
        renderPage={({ pageIndex, width, height }) => {
          const dims = pagePdfDims.get(pageIndex) || { w: width, h: height }
          return (
            <PageView
              documentId={documentId}
              pageIndex={pageIndex}
              width={width}
              height={height}
              hits={hitsByPage.get(pageIndex) || []}
              pagePdfWidth={dims.w}
              pagePdfHeight={dims.h}
            />
          )
        }}
      />
    </Viewport>
  )
}

// ── 顶层 PdfViewer：负责 meta / layout / engine 初始化 + 状态机 ─────────

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

  // 顶层拿不到 useScrollCapability()(它依赖 <EmbedPDF> 上下文)。
  // header 的 PDF 状态显示由 PdfStatus 内部用 useScrollCapability
  // (在 <EmbedPDF> 内部的子组件里) 处理。顶层 PdfStatus 不显示 PDF 状态。
  // 这里只显示 DOCX nav 跳页。

  const apiBase = import.meta.env.VITE_API_BASE_URL || ''
  const pdfUrl =
    meta?.file_type === 'pdf'
      ? `${apiBase}/api/v1/kb-documents/${docId}/file`
      : null

  // ── 文档 meta ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!docId) return
    const ctrl = new AbortController()
    fetch(`${apiBase}/api/v1/kb-documents/${docId}`, { signal: ctrl.signal })
      .then(r => { if (!r.ok) throw new Error('文档不存在'); return r.json() })
      .then(m => setMeta(m))
      .catch(e => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e.message)
      })
      .finally(() => setLoading(false))
    return () => ctrl.abort()
  }, [docId, apiBase])

  // ── layout ───────────────────────────────────────────────────────────
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

  // ── DOCX/MD 文本降级 ────────────────────────────────────────────────
  useEffect(() => {
    if (!meta || meta.file_type === 'pdf') return
    const ctrl = new AbortController()
    const page = Math.max(targetPage - 1, 0)
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/page/${page}`, { signal: ctrl.signal })
      .then(r => {
        if (!r.ok) throw new Error(`text page fetch ${r.status}`)
        return r.json()
      })
      .then(d => { setTextContent(d.text); setTextTotalPages(d.total_pages) })
      .catch(e => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e.message)
      })
    return () => ctrl.abort()
  }, [meta, docId, targetPage, apiBase])

  // ── embedpdf engine + plugins ────────────────────────────────────────
  //
  // worker: false (deferred) — 2026-07 spike 验证 Vite dev 下 inline-blob-worker
  // 初始化卡死,2026-07-09 #65 验证 production build (vite preview) worker: true
  // 同样卡死(`isLoaded=false isLoading=true` 永不变;wasm 永远不被 worker 拉取)。
  // 留 follow-up 给 #62 父 issue 排查 inline-blob-worker + pdfium.wasm 跨
  // worker 上下文 fetch 失败的原因。
  const {
    engine,
    isLoading: engineLoading,
    error: engineError,
  } = usePdfiumEngine({ wasmUrl: '/pdfium.wasm', worker: false })

  const plugins = useMemo(
    () => [
      createPluginRegistration(DocumentManagerPluginPackage, {
        initialDocuments: pdfUrl ? [{ url: pdfUrl, documentId: docId }] : [],
      }),
      createPluginRegistration(ViewportPluginPackage, { viewportGap: 10 }),
      createPluginRegistration(ScrollPluginPackage, {
        defaultStrategy: ScrollStrategy.Vertical,
        defaultPageGap: 10,
        defaultBufferSize: 2,
      }),
      createPluginRegistration(RenderPluginPackage),
    ],
    [pdfUrl, docId],
  )

  // ── E1 重新解析按钮 ──────────────────────────────────────────────────
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
      setError('已提交重新解析，请稍后刷新页面查看 layout')
    } finally {
      setReparsing(false)
    }
  }, [apiBase, docId])

  // ── 顶层状态机 ───────────────────────────────────────────────────────
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

  return (
    <div data-testid="pdf-viewer" className="min-h-screen bg-slate-100">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">{meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {meta.file_type === 'pdf' && !engineLoading && !engineError && engine && (
            <PdfHeaderStatus
              meta={meta}
              targetPage={targetPage}
              onJump={n => updateSearchParam(setSearchParams, 'page', String(n))}
            />
          )}
          {!engineLoading && engineError && (
            <span className="text-xs text-red-500">引擎错误：{engineError.message}</span>
          )}
          {engineLoading && (
            <span className="text-xs text-slate-400">PDF 引擎加载中…</span>
          )}
          {meta.file_type !== 'pdf' && textTotalPages > 0 && (
            <TextNav
              targetPage={targetPage}
              textTotalPages={textTotalPages}
              onJump={n => updateSearchParam(setSearchParams, 'page', String(n))}
            />
          )}
          {highlight && <span className="text-xs text-amber-600 ml-2">🔍 高亮: {highlight.slice(0, 50)}</span>}
          {blockRange && <span className="text-xs text-amber-600 ml-2">📍 block_range: {blockRange.join(',')}</span>}
          {showE1 && (
            <div className="flex items-center gap-2 ml-2 text-xs text-slate-400">
              <span>该文档未解析，无法定位引用位置</span>
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
        <div style={{ height: 'calc(100vh - 57px)' }} className="relative">
          {engineLoading && (
            <div data-testid="engine-loading" className="flex justify-center py-20">
              <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
            </div>
          )}
          {engineError && (
            <div className="text-center py-20 text-red-500">
              PDF 引擎初始化失败：{engineError.message}
            </div>
          )}
          {engine && (
            <EmbedPDF engine={engine} plugins={plugins}>
              {({ activeDocumentId }) => {
                if (!activeDocumentId) {
                  return (
                    <div data-testid="no-active-doc" className="p-4 text-slate-500 text-sm">
                      等待文档加载…
                    </div>
                  )
                }
                return (
                  <DocumentContent documentId={activeDocumentId}>
                    {({ isLoaded }) => {
                      if (!isLoaded) {
                        return (
                          <div className="flex justify-center py-20">
                            <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
                          </div>
                        )
                      }
                      return (
                        <EmbedContent
                          documentId={activeDocumentId}
                          layout={layout.data}
                          blockRange={blockRange}
                          highlight={highlight}
                          urlPage={targetPage}
                          onJump={n => updateSearchParam(setSearchParams, 'page', String(n))}
                        />
                      )
                    }}
                  </DocumentContent>
                )
              }}
            </EmbedPDF>
          )}
        </div>
      ) : (
        <div className="flex justify-center py-6" data-testid="text-fallback">
          <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
            <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {textContent || '（该页无文本内容）'}
            </pre>
            {textTotalPages > 0 && (
              <p className="text-xs text-slate-400 mt-4">第 {targetPage} / {textTotalPages} 页</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── 子组件 ────────────────────────────────────────────────────────────────

/** 顶层 header 上的 PDF 状态(只能在 <EmbedPDF> 外的 React 树渲染)。
 *  这里只显示 doc 元信息(后端的 page_count)+ URL 目标页。
 *  实时 currentPage / 跳转输入框由 EmbedContent 在 <EmbedPDF> 内渲染。
 */
function PdfHeaderStatus(props: {
  meta: DocMeta
  targetPage: number
  onJump: (page: number) => void
}) {
  const { meta, targetPage, onJump } = props
  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span>目标页 {targetPage}</span>
      <span>共 {meta.page_count || '?'} 页</span>
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
