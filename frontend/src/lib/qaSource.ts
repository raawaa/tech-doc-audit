import type { QASource } from '../api/types'

/**
 * 构造 QA 来源的 PDF 预览链接（新标签页打开）。
 *
 * V8-S7 行为变更：
 * - 优先用 `block_range` 坐标（V8-S6 已接入 PdfViewer），URL 形如 `?block_range=2,5`。
 * - `block_range` 缺失时 fallback 到原 `highlight` 字符串匹配路径，保持旧行为。
 * - `page_number` 为 0-based，PDF 预览 page 参数为 1-based（`page_number + 1`）。
 * - `page_number` 为 null（非 PDF / 解析失败）时不带 page 参数（预览默认第 1 页）。
 * - 无 `doc_id` 时返回 null（无法预览）。
 */
export function buildQASourcePreviewUrl(source: QASource): string | null {
  if (!source.doc_id) return null

  const page = source.page_number == null ? '' : String(source.page_number + 1)

  const params = new URLSearchParams()
  if (page !== '') params.set('page', page)

  // V8-S7: block_range 优先；缺失时 fallback 到 highlight 字符串匹配
  if (source.block_range) {
    const [start, end] = source.block_range
    params.set('block_range', `${start},${end}`)
  } else {
    const snippet = (source.content_snippet || '').trim()
    const highlight = snippet.split(/\s+/).filter(Boolean).slice(0, 20).join(' ').slice(0, 40)
    if (highlight) params.set('highlight', highlight)
  }

  const qs = params.toString()
  return `/pdf-viewer/${source.doc_id}${qs ? `?${qs}` : ''}`
}
