import type { QASource } from '../api/types'

/**
 * 构造 QA 来源的 PDF 预览链接（新标签页打开）。
 *
 * - chunk 的 page_number 为 0-based，PDF 预览 page 参数为 1-based，故 page_number + 1。
 * - page_number 为 null（非 PDF / 解析失败）时不带 page 参数（预览默认第 1 页）。
 * - highlight 取 content_snippet 前 ~20 词（按空白分词，整体截断 40 字符，兼容中文无空白分词），避免 URL 过长。
 * - 无 doc_id 时返回 null（无法预览）。
 */
export function buildQASourcePreviewUrl(source: QASource): string | null {
  if (!source.doc_id) return null

  const page = source.page_number == null ? '' : String(source.page_number + 1)
  const snippet = (source.content_snippet || '').trim()
  const highlight = snippet.split(/\s+/).filter(Boolean).slice(0, 20).join(' ').slice(0, 40)

  const params = new URLSearchParams()
  if (page !== '') params.set('page', page)
  if (highlight) params.set('highlight', highlight)
  const qs = params.toString()
  return `/pdf-viewer/${source.doc_id}${qs ? `?${qs}` : ''}`
}
