import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { pdfjs } from 'react-pdf'
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
  const [searchParams] = useSearchParams()
  const targetPage = parseInt(searchParams.get('page') || '1', 10)
  const highlight = searchParams.get('highlight') || ''

  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [currentPage, setCurrentPage] = useState(targetPage)
  const [totalPages, setTotalPages] = useState(0)

  // 获取文档元数据
  useEffect(() => {
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    fetch(`${apiBase}/api/v1/kb-documents/${docId}`)
      .then(r => { if (!r.ok) throw new Error('文档不存在'); return r.json() })
      .then(m => setMeta(m))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [docId])

  // 加载 PDF
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
      setTotalPages(doc.numPages)
    }).catch(e => setError(`PDF 加载失败: ${e.message}`))
  }, [meta, docId])

  // 渲染当前页 + 高亮
  useEffect(() => {
    if (!pdfDoc || !canvasRef.current) return
    const pageNum = Math.min(Math.max(currentPage, 1), totalPages)
    pdfDoc.getPage(pageNum).then(page => {
      const canvas = canvasRef.current!
      const viewport = page.getViewport({ scale: 1.5 })
      canvas.height = viewport.height
      canvas.width = viewport.width
      const ctx = canvas.getContext('2d')!
      page.render({ canvas, viewport }).promise.then(() => {
        if (!highlight || canvas.width === 0) return
        // 搜索并高亮文本
        page.getTextContent().then(textContent => {
          const searchTerms = highlight.split(/\s+/).filter(t => t.length > 1)
          if (searchTerms.length === 0) return
          const scale = 1.5
          for (const item of textContent.items) {
            const textItem = item as { str: string; transform: number[] }
            const str = textItem.str || ''
            for (const term of searchTerms) {
              if (str.includes(term)) {
                const tx = textItem.transform
                const x = tx[4] * scale
                const y = canvas.height - tx[5] * scale
                const w = (str.length * (tx[0] || 8)) * scale * 0.6
                const h = 14
                ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
                ctx.fillRect(x - 1, y - h, w + 2, h + 4)
              }
            }
          }
        })
      })
    })
  }, [pdfDoc, currentPage, highlight, totalPages])

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
          {pdfDoc && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage <= 1} onClick={() => setCurrentPage(p => p - 1)}>←</button>
              <span className="text-slate-600 tabular-nums">{currentPage} / {totalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage >= totalPages} onClick={() => setCurrentPage(p => p + 1)}>→</button>
              <input type="number" className="w-14 px-2 py-1 border rounded text-center text-xs"
                min={1} max={totalPages} value={currentPage}
                onChange={e => { const v = parseInt(e.target.value); if (v >= 1 && v <= totalPages) setCurrentPage(v) }} />
            </>
          )}
          {highlight && <span className="text-xs text-amber-600 ml-2">🔍 高亮: {highlight.slice(0, 50)}</span>}
        </div>
      </div>

      {/* Content */}
      <div className="flex justify-center py-6">
        {meta.file_type === 'pdf' ? (
          <div className="bg-white shadow-lg rounded">
            <canvas ref={canvasRef} className="max-w-full" />
          </div>
        ) : (
          <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
            <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {textContent || '（该页无文本内容）'}
            </pre>
            {textTotalPages > 0 && (
              <p className="text-xs text-slate-400 mt-4">第 {targetPage} / {textTotalPages} 页</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
