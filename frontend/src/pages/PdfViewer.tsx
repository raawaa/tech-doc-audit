import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Document, Page, pdfjs } from 'react-pdf'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import {
  matchBlockRangeToBlocks,
  matchHighlightToBlocks,
  type Block as LayoutBlock,
  type HighlightRect,
} from '../lib/layoutMatch'

// 设置 worker — react-pdf 和 pdfjs-dist 共用同一个 worker
const WORKER_SRC = '/pdfjs/pdf.worker.min.mjs'
pdfjs.GlobalWorkerOptions.workerSrc = WORKER_SRC

/**
 * 服务端 /layout 返回的页面 layout 一项。
 * page 是 0-based，width / height 在这里仅用于 aspect ratio 校验（实际显示
 * 尺寸由 react-pdf <Page> 渲染时的 canvas 决定）。
 */
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

interface DocMeta {
  id: string
  name: string
  file_type: string
  page_count: number | null
}

/** layout fetch 的状态机。 */
interface LayoutState {
  data: LayoutDoc | null
  loading: boolean
  error: 'not-found' | 'other' | null
}

const INITIAL_LAYOUT: LayoutState = { data: null, loading: false, error: null }


/** 解析 ``block_range`` URL 参数 ``"start,end"`` → ``[start, end] | null``。

V8-S6:后端 IssueResponse.standard_block_range 经 AuditResult 跳转时通过 URL
``?block_range=start,end&page=N`` 传递；这里是该路径的入口。
解析失败 / 缺字段 / 反序 → null,走 fallback。
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


export function PdfViewer() {
  const pathname = window.location.pathname
  const docId = pathname.split('/').pop() || ''
  const [searchParams, setSearchParams] = useSearchParams()
  const targetPage = parseInt(searchParams.get('page') || '1', 10)
  const highlight = searchParams.get('highlight') || ''
  // V8-S6:正向 block_range 坐标路径(优先级高于字符串匹配)。
  // 解析失败 / 缺字段 → null → 走 matchHighlightToBlocks fallback。
  const blockRange = parseBlockRangeParam(searchParams.get('block_range'))
  // V8-S6:block_range 路径只画起始页(MVP 限制,与后端 page_number 同语义)。
  const blockRangePage0 = blockRange && targetPage > 0 ? targetPage - 1 : null

  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [numPages, setNumPages] = useState(0)

  // —— 布局数据（V7.3）——
  const [layout, setLayout] = useState<LayoutState>(INITIAL_LAYOUT)
  // —— 重新解析状态（E1 按钮）——
  const [reparsing, setReparsing] = useState(false)

  // —— 绘制层状态（命令式，不触发重渲）——
  // 当前已挂载页的 canvas（Virtuoso 只挂载视口内的页）
  const pageCanvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map())
  // 已画过高亮的页（避免同一画布重复绘制；卸载时清除以便重挂后重画）
  const paintedPages = useRef<Set<number>>(new Set())

  // —— 匹配层产出：仅缓存归一化坐标 + page 索引 ——
  // { 页号 → 命中 block（归一化坐标）}
  const normalizedHitsByPage = useRef<Map<number, HighlightRect[]>>(new Map())
  // 第一个命中所在页号；用于 scrollToIndex
  const firstHitPage = useRef<number | null>(null)

  const virtuosoRef = useRef<VirtuosoHandle>(null)

  const apiBase = import.meta.env.VITE_API_BASE_URL || ''
  const pdfUrl = meta?.file_type === 'pdf' ? `${apiBase}/api/v1/kb-documents/${docId}/file` : null

  // 获取文档元数据
  useEffect(() => {
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    fetch(`${apiBase}/api/v1/kb-documents/${docId}`)
      .then(r => { if (!r.ok) throw new Error('文档不存在'); return r.json() })
      .then(m => setMeta(m))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [docId])

  // mount 时 fetch layout
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
  }, [meta, docId])

  // 追踪 react-pdf 总页数
  function handleDocumentLoadSuccess({ numPages: total }: { numPages: number }) {
    setNumPages(total)
  }

  // 高亮计算：layout + (block_range | highlight) 就绪后一次性算所有命中，
  // **保留归一化坐标**（实际绘制在 page render success 时按 canvas 显示尺寸换算）。
  //
  // V8-S6 路径优先级：
  // 1. ``block_range`` 非空 + layout 已加载 → 走 ``matchBlockRangeToBlocks`` 坐标路径
  //    （仅画起始页，跨页 chunk MVP 限制）。
  // 2. 否则 ``highlight`` 非空 → 走 ``matchHighlightToBlocks`` 字符串匹配 fallback
  //    （旧 KB chunk 兼容）。
  useEffect(() => {
    const useBlockRange = !!(blockRange && layout.data && numPages)
    const useFallback = !!(highlight && layout.data && numPages)

    if (!useBlockRange && !useFallback) {
      normalizedHitsByPage.current = new Map()
      firstHitPage.current = null
      if (pageCanvasRefs.current.size > 0) {
        pageCanvasRefs.current.forEach((_, pageNum) => {
          paintedPages.current.delete(pageNum)
        })
      }
      return
    }
    const allHits = new Map<number, HighlightRect[]>()
    let firstPage: number | null = null

    if (useBlockRange && blockRange && layout.data) {
      // 坐标路径：只在起始页（blockRangePage0）画
      const targetPage0 = blockRangePage0!
      const page = layout.data.layout.find(p => p.page === targetPage0)
      if (page) {
        const pageW = Math.max(page.width, 1)
        const pageH = Math.max(page.height, 1)
        const hits = matchBlockRangeToBlocks(
          blockRange, page.blocks, pageW, pageH, page.page,
        )
        if (hits.length > 0) {
          allHits.set(page.page, hits)
          firstPage = page.page
        }
      }
    } else if (useFallback && highlight && layout.data) {
      // 字符串匹配 fallback
      for (const page of layout.data.layout) {
        const pageW = Math.max(page.width, 1)
        const pageH = Math.max(page.height, 1)
        const hits = matchHighlightToBlocks(
          highlight, page.blocks, pageW, pageH, page.page,
        )
        if (hits.length > 0) {
          allHits.set(page.page, hits)
          if (firstPage === null || page.page < firstPage) firstPage = page.page
        }
      }
    }

    normalizedHitsByPage.current = allHits
    firstHitPage.current = firstPage
    // 让已挂载页重画：fillStyle+fillRect 直接画（layoutMatch 单测覆盖了
  // bbox_norm → 像素换算语义），同时把 paintedPages 重置以便 onRenderSuccess
  // 回调对后续 mount 的页继续画。
    paintedPages.current = new Set()
    pageCanvasRefs.current.forEach((canvas, pageNum) => {
      const hits = allHits.get(pageNum)
      if (!hits) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
      for (const h of hits) ctx.fillRect(h.x, h.y, h.w, h.h)
      paintedPages.current.add(pageNum)
    })
  }, [layout.data, highlight, numPages, blockRange, blockRangePage0])
  // —— 滚动到第一命中页或 URL targetPage ——
  // 用签名（docId + firstHitPage + targetPage + blockRange）作 latch，避免 URL
  // `page=` 改了但没 remount 时不触发；同时 highlight / block_range 命中变化
  // （layout data 换新）也重滚。
  const lastScrollSignatureRef = useRef<string | null>(null)
  useEffect(() => {
    if (numPages <= 0) return
    const signature = `${docId}|${firstHitPage.current}|${targetPage}|${blockRange ? blockRange.join(',') : ''}`
    if (lastScrollSignatureRef.current === signature) return
    lastScrollSignatureRef.current = signature
    const target = firstHitPage.current !== null
      ? firstHitPage.current + 1  // 0-based → 1-based 显示
      : targetPage
    const idx = Math.min(Math.max(target - 1, 0), numPages - 1)
    requestAnimationFrame(() => virtuosoRef.current?.scrollToIndex(idx))
  }, [docId, numPages, targetPage, layout.data, highlight, blockRange])

  // 绘制层：某页渲染完成时画归一化命中（内联 fillStyle+fillRect，与上文
  // 匹配完成时绘制共用同一绘制入口；真正需要被单测的是 layoutMatch 模块）。
  const handlePageRenderSuccess = useCallback((pageNumber: number) => {
    const hits = normalizedHitsByPage.current.get(pageNumber)
    if (!hits || paintedPages.current.has(pageNumber)) return
    const canvas = pageCanvasRefs.current.get(pageNumber)
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
    for (const h of hits) ctx.fillRect(h.x, h.y, h.w, h.h)
    paintedPages.current.add(pageNumber)
  }, [])

  // canvas 注入
  const registerCanvas = useCallback((pageNumber: number) => (ref: HTMLCanvasElement | null) => {
    if (ref) {
      pageCanvasRefs.current.set(pageNumber, ref)
    } else {
      pageCanvasRefs.current.delete(pageNumber)
      paintedPages.current.delete(pageNumber)
    }
  }, [])

  // E1 重新解析按钮
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

  // 页码跳转（PDF 模式）
  const handleJumpToPage = (page: number) => {
    if (numPages <= 0) return
    const clamped = Math.min(Math.max(page, 1), numPages)
    virtuosoRef.current?.scrollToIndex(clamped - 1)
  }

  // 文本降级模式（DOCX/MD）
  const [textContent, setTextContent] = useState('')
  const [textTotalPages, setTextTotalPages] = useState(0)

  useEffect(() => {
    if (!meta || meta.file_type === 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const page = Math.max(targetPage - 1, 0)
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/page/${page}`)
      .then(r => r.json())
      .then(d => { setTextContent(d.text); setTextTotalPages(d.total_pages) })
      .catch(e => setError(e.message))
  }, [meta, docId, targetPage])

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (error) return <div className="text-center py-20 text-red-500">{error}</div>
  if (!meta) return <div className="text-center py-20 text-slate-500">文档不存在</div>

  const showE1 = layout.error === 'not-found' && !!highlight
  const showE2 = layout.error === 'other' && !!highlight

  return (
    <div className="min-h-screen bg-slate-100">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">{meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {numPages > 0 && (
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <span>跳至</span>
              <input
                type="number"
                className="w-14 px-2 py-1 border rounded text-center text-xs"
                min={1}
                max={numPages}
                defaultValue={targetPage}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    handleJumpToPage(parseInt((e.target as HTMLInputElement).value))
                  }
                }}
              />
              <span>页 / 共 {numPages} 页</span>
            </div>
          )}
          {!numPages && textTotalPages > 0 && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage <= 1}
                onClick={() => setSearchParams({ page: String(targetPage - 1) })}>←</button>
              <span className="text-slate-600 tabular-nums text-sm">{targetPage} / {textTotalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage >= textTotalPages}
                onClick={() => setSearchParams({ page: String(targetPage + 1) })}>→</button>
            </>
          )}
          {highlight && <span className="text-xs text-amber-600 ml-2">🔍 高亮: {highlight.slice(0, 50)}</span>}
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
        <div style={{ height: 'calc(100vh - 57px)' }}>
          {pdfUrl && (
            <Document
              className="h-full"
              file={pdfUrl}
              onLoadSuccess={handleDocumentLoadSuccess}
              loading={
                <div className="flex justify-center py-20">
                  <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
                </div>
              }
              error={
                <div className="text-center py-20 text-red-500">
                  PDF 加载失败，请刷新重试
                </div>
              }
            >
              <Virtuoso
                ref={virtuosoRef}
                totalCount={numPages}
                defaultItemHeight={1100}
                increaseViewportBy={{ top: 800, bottom: 800 }}
                style={{ height: '100%' }}
                itemContent={index => {
                  const pageNumber = index + 1
                  return (
                    <div
                      key={`page_${pageNumber}`}
                      data-page-number={pageNumber}
                      className="flex justify-center py-2"
                    >
                      <Page
                        pageNumber={pageNumber}
                        canvasRef={registerCanvas(pageNumber)}
                        onRenderSuccess={() => handlePageRenderSuccess(pageNumber)}
                        renderTextLayer={false}
                        className="bg-white shadow-lg rounded"
                      />
                    </div>
                  )
                }}
              />
            </Document>
          )}
        </div>
      ) : (
        <div className="flex justify-center py-6">
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

