import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import * as pdfjsLib from 'pdfjs-dist'

// 设置 worker — react-pdf 和 pdfjs-dist 共用同一个 worker
const WORKER_SRC = '/pdfjs/pdf.worker.min.mjs'
pdfjs.GlobalWorkerOptions.workerSrc = WORKER_SRC
pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER_SRC

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
  const [allPagesRendered, setAllPagesRendered] = useState(false)
  const pageRefs = useRef<Map<number, HTMLCanvasElement>>(new Map())
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const [renderedPages, setRenderedPages] = useState(0)
  void renderedPages
  const highlightAppliedRef = useRef('')

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

  // 追踪页面渲染完成
  const handlePageRenderSuccess = useCallback((_pageNumber: number) => {
    setRenderedPages(prev => {
      const next = prev + 1
      // 当所有页面渲染完成时标记
      if (next >= numPages) {
        // 使用 setTimeout 确保状态更新完成
        setTimeout(() => setAllPagesRendered(true), 100)
      }
      return next
    })
  }, [numPages])

  // 重置渲染计数器（PDF 文档变化时）
  useEffect(() => {
    setRenderedPages(0)
    setAllPagesRendered(false)
    pageRefs.current.clear()
    highlightAppliedRef.current = ''
  }, [numPages])

  // 页码跳转（PDF 模式）
  const handleJumpToPage = (page: number) => {
    if (!scrollContainerRef.current) return
    const clamped = Math.min(Math.max(page, 1), numPages)
    const pageEl = scrollContainerRef.current.querySelector(
      `[data-page-number="${clamped}"]`
    )
    if (pageEl) {
      pageEl.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
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

  // 高亮搜索与自动定位
  useEffect(() => {
    if (!pdfDoc || !highlight || !allPagesRendered) return
    if (highlight === highlightAppliedRef.current) return
    highlightAppliedRef.current = highlight

    const doc = pdfDoc
    const searchTerms = highlight.split(/\s+/).filter(t => t.length > 1)
    if (searchTerms.length === 0) return

    let cancelled = false
    let firstHighlightedPage: number | null = null

    // 逐页搜索文本
    async function searchAndHighlight() {
      for (let pageNum = 1; pageNum <= numPages; pageNum++) {
        if (cancelled) return
        try {
          const page = await doc.getPage(pageNum)
          const textContent = await page.getTextContent()

          let pageHasMatch = false
          const canvas = pageRefs.current.get(pageNum)
          if (!canvas) continue

          const ctx = canvas.getContext('2d')
          if (!ctx) continue

          for (const item of textContent.items) {
            const textItem = item as { str: string; transform: number[] }
            const str = textItem.str || ''
            for (const term of searchTerms) {
              if (str.includes(term)) {
                pageHasMatch = true
                const tx = textItem.transform
                const scale = 1.5
                const x = tx[4] * scale
                const y = canvas.height - tx[5] * scale
                const w = (str.length * (tx[0] || 8)) * scale * 0.6
                const h = 14
                ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
                ctx.fillRect(x - 1, y - h, w + 2, h + 4)
              }
            }
          }

          if (pageHasMatch && firstHighlightedPage === null) {
            firstHighlightedPage = pageNum
          }
        } catch {
          // 跳过渲染失败的页面
        }
      }

      // 自动滚动到第一个有高亮的页面
      if (firstHighlightedPage !== null && scrollContainerRef.current) {
        const pageEl = scrollContainerRef.current.querySelector(
          `[data-page-number="${firstHighlightedPage}"]`
        )
        if (pageEl) {
          pageEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }
      }
    }

    searchAndHighlight()
    return () => { cancelled = true }
  }, [pdfDoc, highlight, numPages, allPagesRendered])

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
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-auto"
        style={{ height: 'calc(100vh - 57px)' }}
      >
        {meta.file_type === 'pdf' ? (
          <div className="flex flex-col items-center py-6 gap-4">
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
                {Array.from(new Array(numPages), (_, index) => (
                  <div key={`page_${index + 1}`} data-page-number={index + 1}>
                    <Page
                      pageNumber={index + 1}
                      canvasRef={(ref: HTMLCanvasElement) => {
                        if (ref) {
                          pageRefs.current.set(index + 1, ref)
                        }
                      }}
                      onRenderSuccess={() => handlePageRenderSuccess(index + 1)}
                      renderTextLayer={false}
                      className="bg-white shadow-lg rounded"
                    />
                  </div>
                ))}
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
    </div>
  )
}
