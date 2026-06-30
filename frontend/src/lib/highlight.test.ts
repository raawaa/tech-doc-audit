import { describe, it, expect } from 'vitest'
import {
  computePageMatches,
  paintHighlight,
  type HighlightMatch,
  type PdfTextItem,
} from './highlight'

describe('computePageMatches', () => {
  const term = ['规范']

  it('returns nothing when no text items match any term', () => {
    const items: PdfTextItem[] = [
      { str: '无关文本', transform: [8, 0, 0, 8, 10, 20] },
    ]
    expect(computePageMatches(items, term, 1.5)).toEqual([])
  })

  it('bakes render scale into canvas-px coords for a matching item', () => {
    // transform = [a, b, c, d, e, f] -> a=charWidth(8), e=x(10), f=y(20)
    const items: PdfTextItem[] = [
      { str: '设计规范要求', transform: [8, 0, 0, 8, 10, 20] },
    ]
    const matches = computePageMatches(items, term, 1.5)

    expect(matches).toHaveLength(1)
    const m = matches[0]
    // x = e*scale - 1 = 10*1.5 - 1 = 14
    expect(m.x).toBe(14)
    // width = str.length*(a||8)*scale*0.6 + 2 = 6*8*1.5*0.6 + 2 = 45.2
    expect(m.width).toBeCloseTo(45.2, 5)
    // height fixed
    expect(m.height).toBe(18)
    // bottomY = f*scale = 20*1.5 = 30
    expect(m.bottomY).toBe(30)
  })

  it('matches when ANY term is a substring (multi-term OR)', () => {
    const items: PdfTextItem[] = [
      { str: '防火', transform: [8, 0, 0, 8, 0, 0] },
      { str: '安全', transform: [8, 0, 0, 8, 0, 0] },
    ]
    const matches = computePageMatches(items, ['防火', '疏散'], 1)
    expect(matches).toHaveLength(1) // only 防火 matches
  })

  it('ignores terms shorter than 2 chars', () => {
    const items: PdfTextItem[] = [
      { str: 'a规范', transform: [8, 0, 0, 8, 0, 0] },
    ]
    // single-char term 'a' must be ignored; only 规范 counts
    const matches = computePageMatches(items, ['a', '规范'], 1)
    expect(matches).toHaveLength(1)
  })

  it('deduplicates a single item that hits multiple terms', () => {
    const items: PdfTextItem[] = [
      { str: '防火规范', transform: [8, 0, 0, 8, 5, 5] },
    ]
    const matches = computePageMatches(items, ['防火', '规范'], 1)
    expect(matches).toHaveLength(1)
  })
})

describe('paintHighlight', () => {
  function mockCanvas(height: number) {
    const calls: Array<{ x: number; y: number; w: number; h: number }> = []
    const ctx = {
      fillStyle: '',
      fillRect(x: number, y: number, w: number, h: number) {
        calls.push({ x, y, w, h })
      },
    }
    const canvas = {
      height,
      getContext: () => ctx,
    } as unknown as HTMLCanvasElement
    return { canvas, calls, ctx }
  }

  it('does nothing when canvas is null', () => {
    expect(() => paintHighlight(null, 1, [])).not.toThrow()
  })

  it('does nothing when there are no matches', () => {
    const { canvas, calls } = mockCanvas(800)
    paintHighlight(canvas, 1, [])
    expect(calls).toEqual([])
  })

  it('flips y using canvas.height and draws a rect per match', () => {
    const { canvas, calls } = mockCanvas(800)
    const matches: HighlightMatch[] = [
      { x: 14, width: 45.2, height: 18, bottomY: 30 },
    ]
    paintHighlight(canvas, 2, matches)
    expect(calls).toHaveLength(1)
    // y = canvas.height - bottomY - ASCENT(14) = 800 - 30 - 14 = 756
    expect(calls[0]).toEqual({ x: 14, y: 756, w: 45.2, h: 18 })
  })

  it('uses yellow translucent fill', () => {
    const { canvas, ctx } = mockCanvas(800)
    paintHighlight(canvas, 1, [{ x: 0, width: 10, height: 18, bottomY: 0 }])
    expect(ctx.fillStyle).toContain('255, 255, 0')
  })
})
