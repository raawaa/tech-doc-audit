import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Document, Page, pdfjs } from 'react-pdf'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import * as pdfjsLib from 'pdfjs-dist'
import { computePageMatches, paintHighlight, type HighlightMatch } from '../lib/highlight'

// 设置 worker — react-pdf 和 pdfjs-dist 共用同一个 worker
const WORKER_SRC = '/pdfjs/pdf.worker.min.mjs'
pdfjs.GlobalWorkerOptions.workerSrc = WORKER_SRC
pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER_SRC

/**
 * 渲染缩放 = react-pdf <Page> 的 scale(默认 1) × devicePixelRatio。
 * react-pdf 把 canvas 内部分辨率设为 `磅 × scale × devicePixelRatio`，
 * 故高亮坐标换算需用此值（见 lib/highlight.ts）。
 */
const RENDER_SCALE =
  typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1

interface DocMeta {
  id: string
  name: string
  file_type: string
  page_count: number | null
}

export function PdfViewer() {
  const pathname = window.location.pathname
  const docId = pathname.split('/').pop() || ''
  const [searchParams, setSearchParams] = useSearchParams()
  const targetPage = parseInt(searchParams.get('page') || '1', 10)
  const highlight = searchParams.get('highlight') || ''

  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [numPages, setNumPages] = useState(0)
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null)

  // —— 绘制层状态（命令式，不触发重渲）——
  // 当前已挂载页的 canvas（Virtuoso 只挂载视口内的页）
  const pageCanvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map())
  // 已画过高亮的页（避免同一画布重复绘制；卸载时清除以便重挂后重画）
  const paintedPages = useRef<Set<number>>(new Set())

  // —— 匹配层状态 ——
  // { 页号 → 磅坐标命中[] }，匹配层异步增量写入
  const matchesByPage = useRef<Map<number, HighlightMatch[]>>(new Map())

  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const didInitialScrollRef = useRef(false)

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

  // 并行加载 PDF 文档用于文本搜索（浏览器缓存确保只下载一次）
  useEffect(() => {
    if (!meta || meta.file_type !== 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const url = `${apiBase}/api/v1/kb-documents/${docId}/file`
    pdfjsLib.getDocument({
      url,
      cMapUrl: '/cmaps/',
      cMapPacked: true,
      wasmUrl: '/pdfjs/wasm/',
    }).promise.then(doc => {
      setPdfDoc(doc)
    }).catch(e => setError(`PDF 加载失败: ${e.message}`))
  }, [meta, docId])

  // 追踪 react-pdf 总页数
  function handleDocumentLoadSuccess({ numPages: total }: { numPages: number }) {
    setNumPages(total)
  }

  // —— URL `page` 参数独占定位：numPages 就绪后立即滚动，不等匹配层 ——
  useEffect(() => {
    if (numPages <= 0 || didInitialScrollRef.current) return
    didInitialScrollRef.current = true
    const idx = Math.min(Math.max(targetPage - 1, 0), numPages - 1)
    //下一帧再滚，确保 Virtuoso 已挂载
    requestAnimationFrame(() => virtuosoRef.current?.scrollToIndex(idx))
  }, [numPages, targetPage])

  // 匹配层：异步遍历各页文本，增量写入 matchesByPage 并对"已挂载但未画"的页补画。
  // 不再依赖"所有页渲染完"闸门；不再自动滚动到首个匹配页。
  useEffect(() => {
    if (!pdfDoc || !highlight || numPages <= 0) return
    const searchTerms = highlight.split(/\s+/).filter(t => t.length > 1)
    if (searchTerms.length === 0) return

    // 重置该次搜索的匹配与绘制标记
    matchesByPage.current = new Map()
    paintedPages.current = new Set()

    let cancelled = false
    const doc = pdfDoc

    async function searchAndHighlight() {
      for (let pageNum = 1; pageNum <= numPages; pageNum++) {
        if (cancelled) return
        try {
          const page = await doc.getPage(pageNum)
          if (cancelled) return
          const textContent = await page.getTextContent()
          if (cancelled) return
          const matches = computePageMatches(
            textContent.items as Array<{ str: string; transform: number[] }>,
            searchTerms,
            RENDER_SCALE,
          )
          if (matches.length > 0) {
            matchesByPage.current.set(pageNum, matches)
            // 补画：若该页 canvas 已挂载且尚未画，立即画
            const canvas = pageCanvasRefs.current.get(pageNum)
            if (canvas && !paintedPages.current.has(pageNum)) {
              paintHighlight(canvas, pageNum, matches)
              paintedPages.current.add(pageNum)
            }
          }
        } catch {
          // 跳过读取失败的页面
        }
      }
    }

    searchAndHighlight()
    return () => { cancelled = true }
  }, [pdfDoc, highlight, numPages])

  // 绘制层：某页渲染完成时，若已有匹配则画高亮（覆盖首屏渲染与离屏页重挂两种路径）
  const handlePageRenderSuccess = useCallback((pageNumber: number) => {
    const matches = matchesByPage.current.get(pageNumber)
    if (!matches || paintedPages.current.has(pageNumber)) return
    const canvas = pageCanvasRefs.current.get(pageNumber)
    if (!canvas) return
    paintHighlight(canvas, pageNumber, matches)
    paintedPages.current.add(pageNumber)
  }, [])

  // canvas 注入：注册/注销当前挂载页的 canvas。
  // 注意：不要在此绘制——react-pdf 尚未画完，会被覆盖。绘制统一在 onRenderSuccess。
  const registerCanvas = useCallback((pageNumber: number) => (ref: HTMLCanvasElement | null) => {
    if (ref) {
      pageCanvasRefs.current.set(pageNumber, ref)
    } else {
      pageCanvasRefs.current.delete(pageNumber)
      // 卸载后清除绘制标记，使该页重新挂载时能被重画
      paintedPages.current.delete(pageNumber)
    }
  }, [])

  // 页码跳转（PDF 模式）：交给 Virtuoso 定位
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
    const page = Math.max(targetPage - 1, 0)  // URL is 1-based, API is 0-based
    fetch(`${apiBase}/api/v1/kb-documents/${docId}/page/${page}`)
      .then(r => r.json())
      .then(d => { setTextContent(d.text); setTextTotalPages(d.total_pages) })
      .catch(e => setError(e.message))
  }, [meta, docId, targetPage])

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (error) return <div className="text-center py-20 text-red-500">{error}</div>
  if (!meta) return <div className="text-center py-20 text-slate-500">文档不存在</div>

  return (
    <div className="min-h-screen bg-slate-100">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-white border-b border-slate-200 px-4 py-3 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-sm font-semibold text-slate-800">{meta.name}</h1>
          <p className="text-xs text-slate-400">{meta.file_type.toUpperCase()} · {meta.page_count || '?'} 页</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {/* PDF 模式：页码跳转 */}
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
          {/* 非 PDF 模式：翻页按钮 */}
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
        </div>
      </div>

      {/* Content */}
      {meta.file_type === 'pdf' ? (
        <div style={{ height: 'calc(100vh - 57px)' }}>
          {pdfUrl && (
            <Document
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
