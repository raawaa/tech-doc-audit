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
})
