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

// ── V9 PRD #67 — source-document UIMessage part 形态 ──────────────────────────
//
// 与后端 `api/routers/qa.py:build_source_id / build_source_document_payload`
// 共用相同的 sourceId 形态（后端 emit 时已附 sourceId，前端独立计算仅供
// 单元测试与离线场景）。AI SDK v6 用 sourceId 跨 message 去重。

/**
 * 短 doc_id（前端仅做"看起来像"的占位）。
 *
 * ⚠️ **后端权威**：浏览器环境没有 md5/hashlib，本函数用 djb2 占位，会与
 * 后端 `_short_doc_id`（md5[:8]）算出的 hash **不同**。这意味着前端
 * `buildQASourceId()` 的结果**不应**被用于生产环境去重——实际 sourceId
 * 由后端 SSE 事件携带（`sourceId` 字段），前端照单全收即可。
 *
 * 本函数仅供单元测试 / Storybook / 离线渲染构造稳定 sourceId 用：
 * 1. 测试断言 sourceId 形态稳定（`src_<8hex>_p<page>`）；
 * 2. 离线场景无后端 SSE 时按同形态生成。
 *
 * 若未来需要前端独立算 sourceId，必须替换为与后端 md5 等价的同步实现
 * （如 blueimp-md5），并在测试中固化 md5 期望值。
 */
function shortDocId(docId: string): string {
  if (!docId) return 'empty'
  // djb2 占位实现 — 非加密 hash，**仅**用于本地占位 / 测试。
  let h = 5381
  for (let i = 0; i < docId.length; i++) {
    h = (h * 33 + docId.charCodeAt(i)) >>> 0
  }
  return h.toString(16).padStart(8, '0').slice(0, 8)
}

/** 与后端 `build_source_id` 同形 — `src_<doc_id_short>_p<page>`（page 1-based）。 */
export function buildQASourceId(source: QASource): string {
  const docId = source.doc_id || ''
  const pageToken = `p${typeof source.page_number === 'number' ? source.page_number + 1 : 0}`
  return `src_${shortDocId(docId)}_${pageToken}`
}

/**
 * AI SDK v6 source-document part 的渲染形态。
 * 与后端 `build_source_document_payload` 字段对齐：
 * - type / sourceId / mediaType / title / filename?
 * - providerMetadata.qaSource 携带原 QASource，前端调 buildQASourcePreviewUrl
 */
export type SourceDocumentPart = {
  type: 'source-document'
  sourceId: string
  mediaType: string
  title: string
  filename?: string
  providerMetadata: {
    qaSource: QASource
  }
}

/**
 * 由 QASource 构造 source-document part。`doc_id` 为空时返回 null
 * （search_kb_text 等无 doc_id 来源不应渲染 chip）。
 */
export function buildQASourcePayload(source: QASource): SourceDocumentPart | null {
  if (!source.doc_id) return null
  const part: SourceDocumentPart = {
    type: 'source-document',
    sourceId: buildQASourceId(source),
    mediaType: 'application/pdf',
    title: source.doc_source || '未知来源',
    providerMetadata: { qaSource: source },
  }
  if (source.doc_id) part.filename = source.doc_id
  return part
}

/**
 * 从 AI SDK 流式 UIMessage part 中提取原 QASource（chip 渲染时用）。
 * 非 source-document / providerMetadata.qaSource 缺失时返回 null。
 */
export function extractQASourceFromPart(part: unknown): QASource | null {
  if (!part || typeof part !== 'object') return null
  const p = part as Record<string, unknown>
  if (p.type !== 'source-document') return null
  const meta = p.providerMetadata as { qaSource?: unknown } | undefined
  const qa = meta?.qaSource
  if (!qa || typeof qa !== 'object') return null
  return qa as QASource
}