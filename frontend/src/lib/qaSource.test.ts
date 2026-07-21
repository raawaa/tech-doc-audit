import { describe, it, expect } from 'vitest'
import {
  buildQASourcePreviewUrl,
  buildQASourcePayload,
  buildQASourceId,
  extractQASourceFromPart,
} from './qaSource'
import type { QASource } from '../api/types'

function src(over: Partial<QASource>): QASource {
  return { kb_id: '', doc_id: '', doc_source: '', content_snippet: '', relevance: 0, ...over }
}

describe('buildQASourcePreviewUrl', () => {
  it('returns null when doc_id is missing', () => {
    expect(buildQASourcePreviewUrl(src({ doc_id: '' }))).toBeNull()
  })

  it('converts 0-based page_number to 1-based page param', () => {
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: 4, content_snippet: '质保期片段' }))
    expect(url).toContain('/pdf-viewer/d1?')
    expect(url).toContain('page=5')
    expect(url).toContain('highlight=')
  })

  it('uses page=1 when page_number is 0', () => {
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: 0 }))
    expect(url).toContain('page=1')
  })

  it('omits page param when page_number is null', () => {
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: null }))
    expect(url).not.toContain('page=')
  })

  it('encodes highlight and truncates to ~20 words', () => {
    const long = Array.from({ length: 30 }, (_, i) => `word${i}`).join(' ')
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: 0, content_snippet: long }))
    expect(url).toContain('highlight=word0')
    expect(url).not.toContain('word29')
  })

  it('omits highlight when content_snippet empty', () => {
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: 0, content_snippet: '' }))
    expect(url).not.toContain('highlight=')
  })

  // ── V8-S7: block_range 优先路径 ──────────────────────────────────────────────────


  it('uses block_range when present and omits highlight', () => {
    // V8-S7 核心契约: block_range 非空时 URL 走坐标路径，不带 highlight
    const url = buildQASourcePreviewUrl(src({
      doc_id: 'd1', page_number: 15, content_snippet: 'some highlight text',
      block_range: [2, 5],
    }))
    expect(url).toContain('/pdf-viewer/d1?')
    expect(url).toContain('page=16')
    expect(url).toContain('block_range=2%2C5')  // URLSearchParams 编码逗号
    expect(url).not.toContain('highlight=')
  })

  it('falls back to highlight when block_range is null', () => {
    const url = buildQASourcePreviewUrl(src({
      doc_id: 'd1', page_number: 0,
      content_snippet: 'fallback text',
      block_range: null,
    }))
    expect(url).toContain('highlight=fallback')
    expect(url).not.toContain('block_range=')
  })

  it('falls back to highlight when block_range is undefined', () => {
    const url = buildQASourcePreviewUrl(src({
      doc_id: 'd1', page_number: 0,
      content_snippet: 'fallback text',
    }))
    expect(url).toContain('highlight=fallback')
    expect(url).not.toContain('block_range=')
  })

  it('block_range takes precedence over content_snippet', () => {
    // 即便 content_snippet 存在, block_range 优先
    const url = buildQASourcePreviewUrl(src({
      doc_id: 'd1', page_number: 0,
      content_snippet: 'long snippet text',
      block_range: [0, 1],
    }))
    expect(url).toContain('block_range=0%2C1')
    expect(url).not.toContain('highlight=')
  })

  it('omits both block_range and highlight when source is empty', () => {
    const url = buildQASourcePreviewUrl(src({ doc_id: 'd1', page_number: 0 }))
    expect(url).not.toContain('block_range=')
    expect(url).not.toContain('highlight=')
  })
})

// ── V9 PRD #67: buildQASourcePayload / buildQASourceId / extract ───────────────

describe('buildQASourceId', () => {
  it('format is src_<8hex>_p<page-1-based>', () => {
    // 0-based page 4 → p5
    const id = buildQASourceId(src({ doc_id: 'abc123', page_number: 4 }))
    expect(id).toMatch(/^src_[0-9a-f]{8}_p5$/)
  })

  it('uses p0 when page_number is null', () => {
    const id = buildQASourceId(src({ doc_id: 'abc123', page_number: null }))
    expect(id).toMatch(/^src_[0-9a-f]{8}_p0$/)
  })

  it('uses p0 when page_number is undefined', () => {
    const id = buildQASourceId(src({ doc_id: 'abc123' }))
    expect(id).toMatch(/^src_[0-9a-f]{8}_p0$/)
  })

  it('uses src_empty_p0 for empty doc_id', () => {
    const id = buildQASourceId(src({ doc_id: '' }))
    expect(id).toBe('src_empty_p0')
  })

  it('is stable across calls (same input → same sourceId)', () => {
    const a = buildQASourceId(src({ doc_id: 'X', page_number: 2 }))
    const b = buildQASourceId(src({ doc_id: 'X', page_number: 2 }))
    expect(a).toBe(b)
  })

  it('differs between distinct doc_ids', () => {
    const a = buildQASourceId(src({ doc_id: 'A', page_number: 0 }))
    const b = buildQASourceId(src({ doc_id: 'B', page_number: 0 }))
    expect(a).not.toBe(b)
  })
})

describe('buildQASourcePayload', () => {
  it('returns null when doc_id is empty (search_kb_text 场景)', () => {
    expect(buildQASourcePayload(src({ doc_id: '', page_number: 0 }))).toBeNull()
  })

  it('emits a source-document part with title=doc_source and mediaType=application/pdf', () => {
    const part = buildQASourcePayload(src({
      doc_id: 'd1', doc_source: 'GB/T 12345', page_number: 4,
    }))
    expect(part).not.toBeNull()
    expect(part!.type).toBe('source-document')
    expect(part!.mediaType).toBe('application/pdf')
    expect(part!.title).toBe('GB/T 12345')
    expect(part!.filename).toBe('d1')
  })

  it('falls back to "未知来源" title when doc_source empty', () => {
    const part = buildQASourcePayload(src({ doc_id: 'd1', doc_source: '', page_number: 0 }))
    expect(part!.title).toBe('未知来源')
  })

  it('omits filename when doc_id empty (already null path)', () => {
    // 双重保险：doc_id 缺失时整个 payload 为 null，不存在"无 filename 的 part"
    expect(buildQASourcePayload(src({ doc_id: '' }))).toBeNull()
  })

  it('embeds the original QASource in providerMetadata.qaSource (single source of truth)', () => {
    const original = src({
      doc_id: 'd1', doc_source: '标准 A', page_number: 3,
      content_snippet: '原文片段', relevance: 0.87, block_range: [1, 2],
    })
    const part = buildQASourcePayload(original)
    expect(part!.providerMetadata.qaSource).toEqual(original)
  })

  it('sourceId is stable and matches buildQASourceId()', () => {
    const source = src({ doc_id: 'd1', page_number: 2 })
    const part = buildQASourcePayload(source)!
    expect(part.sourceId).toBe(buildQASourceId(source))
  })

  // 四种情形：doc_id 存在 vs 空 × page_number null vs 非 null
  it('doc_id present + page_number null → payload with p0 sourceId', () => {
    const part = buildQASourcePayload(src({ doc_id: 'd1', page_number: null }))!
    expect(part.sourceId).toMatch(/_p0$/)
    expect(part.filename).toBe('d1')
  })

  it('doc_id present + page_number 0 → payload with p1 sourceId', () => {
    const part = buildQASourcePayload(src({ doc_id: 'd1', page_number: 0 }))!
    expect(part.sourceId).toMatch(/_p1$/)
  })

  it('doc_id empty + page_number null → null', () => {
    expect(buildQASourcePayload(src({ doc_id: '', page_number: null }))).toBeNull()
  })

  it('doc_id empty + page_number present → null (doc_id 优先)', () => {
    expect(buildQASourcePayload(src({ doc_id: '', page_number: 5 }))).toBeNull()
  })

  it('with block_range → qaSource.block_range 透传', () => {
    const part = buildQASourcePayload(src({
      doc_id: 'd1', page_number: 0, block_range: [4, 9],
    }))!
    expect(part.providerMetadata.qaSource.block_range).toEqual([4, 9])
  })

  it('without block_range → qaSource.block_range 为 undefined', () => {
    const part = buildQASourcePayload(src({ doc_id: 'd1', page_number: 0 }))!
    expect(part.providerMetadata.qaSource.block_range).toBeUndefined()
  })
})

describe('extractQASourceFromPart', () => {
  it('extracts qaSource from a source-document part', () => {
    const original = src({ doc_id: 'd1', page_number: 0 })
    const part = buildQASourcePayload(original)!
    expect(extractQASourceFromPart(part)).toEqual(original)
  })

  it('returns null for non-source-document parts', () => {
    expect(extractQASourceFromPart({ type: 'text', text: 'hello' })).toBeNull()
    expect(extractQASourceFromPart({ type: 'reasoning', text: 'thinking' })).toBeNull()
  })

  it('returns null when providerMetadata.qaSource missing', () => {
    expect(extractQASourceFromPart({ type: 'source-document', sourceId: 'x' })).toBeNull()
  })

  it('returns null for null / non-object input', () => {
    expect(extractQASourceFromPart(null)).toBeNull()
    expect(extractQASourceFromPart(undefined)).toBeNull()
    expect(extractQASourceFromPart('source-document')).toBeNull()
    expect(extractQASourceFromPart(42)).toBeNull()
  })
})
