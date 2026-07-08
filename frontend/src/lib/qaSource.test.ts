import { describe, it, expect } from 'vitest'
import { buildQASourcePreviewUrl } from './qaSource'
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
