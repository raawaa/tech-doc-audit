import { describe, it, expect } from 'vitest'
import {
  matchBlockRangeToBlocks,
  matchHighlightToBlocks,
  type Block,
} from './layoutMatch'

// V8-S6 单测覆盖:
// 1. matchBlockRangeToBlocks 坐标路径正确性
// 2. 单 block / 多 block 区间 / 越界 / 缺 bbox_norm 跳过
// 3. parseBlockRangeParam(在 PdfViewer 中,这里通过黑盒验证 URL 参数语义)


const W = 1000
const H = 2000

function makeBlock(block_content: string, bbox: [number, number, number, number], block_order: number): Block {
  return {
    block_label: 'text',
    block_content,
    bbox_norm: bbox,
    block_order,
  }
}


describe('matchBlockRangeToBlocks (V8-S6 坐标高亮主路径)', () => {
  it('单 block 区间:start==end → 命中 1 个 block 的 bbox', () => {
    const blocks = [
      makeBlock('无关', [0.1, 0.1, 0.9, 0.2], 0),
      makeBlock('目标条款', [0.1, 0.3, 0.9, 0.4], 3),
      makeBlock('无关', [0.1, 0.5, 0.9, 0.6], 4),
    ]
    const hits = matchBlockRangeToBlocks([3, 3], blocks, W, H, 0)
    expect(hits).toHaveLength(1)
    expect(hits[0].x).toBeCloseTo(100, 5)
    expect(hits[0].y).toBeCloseTo(600, 5)
    expect(hits[0].w).toBeCloseTo(800, 5)
    expect(hits[0].h).toBeCloseTo(200, 5)
  })

  it('多 block 区间:start<end → 命中多个 block 的 bbox', () => {
    const blocks = [
      makeBlock('条款开头', [0.1, 0.1, 0.9, 0.2], 1),
      makeBlock('条款中间', [0.1, 0.2, 0.9, 0.3], 2),
      makeBlock('条款结尾', [0.1, 0.3, 0.9, 0.4], 3),
      makeBlock('无关', [0.1, 0.4, 0.9, 0.5], 4),
    ]
    const hits = matchBlockRangeToBlocks([1, 3], blocks, W, H, 0)
    expect(hits).toHaveLength(3)
    // 三段 y 不同,顺序按 block_order 升序
    expect(hits[0].y).toBe(200)
    expect(hits[1].y).toBe(400)
    expect(hits[2].y).toBe(600)
  })

  it('block_order 乱序传入:仍按区间筛,不看数组顺序', () => {
    const blocks = [
      makeBlock('a', [0, 0, 1, 0.1], 0),
      makeBlock('b', [0, 0.5, 1, 0.6], 5),
      makeBlock('c', [0, 0.2, 1, 0.3], 2),
      makeBlock('d', [0, 0.7, 1, 0.8], 7),
    ]
    const hits = matchBlockRangeToBlocks([2, 5], blocks, W, H, 0)
    expect(hits).toHaveLength(2)
    // 命中 block_order 2(c,y=0.2→400) 和 5(b,y=0.5→1000),按输入顺序产出。
    // 验证:命中的是 "b"(y=1000) 和 "c"(y=400)——而非其它无关 block。
    const ys = hits.map(h => Math.round(h.y)).sort((a, b) => a - b)
    expect(ys).toEqual([400, 1000])
  })

  it('区间无命中:返回空数组(不应抛)', () => {
    const blocks = [makeBlock('内容', [0, 0, 1, 0.1], 0)]
    expect(matchBlockRangeToBlocks([10, 20], blocks, W, H)).toHaveLength(0)
  })

  it('block 缺 bbox_norm 长度不是 4:跳过该 block,不抛', () => {
    const blocks = [
      { block_label: 'text', block_content: 'a', bbox_norm: [0.1, 0.2, 0.9] as unknown as number[], block_order: 0 },
      makeBlock('b', [0.1, 0.3, 0.9, 0.4], 1),
    ]
    const hits = matchBlockRangeToBlocks([0, 1], blocks, W, H, 0)
    // 只有第二个 block 命中(第一个 bbox_norm 不合法)
    expect(hits).toHaveLength(1)
    expect(hits[0].y).toBe(600)
  })

  it('block 缺 block_order 字段:order=-1,不命中正区间(防御性)', () => {
    const blocks = [
      { block_label: 'text', block_content: 'a', bbox_norm: [0, 0, 1, 0.1] },  // 无 block_order
    ]
    expect(matchBlockRangeToBlocks([0, 5], blocks, W, H)).toHaveLength(0)
  })

  it('bbox_norm 含 NaN:跳过该 block', () => {
    const blocks = [
      { block_label: 'text', block_content: 'a', bbox_norm: [NaN, 0, 1, 0.1] as unknown as number[], block_order: 0 },
    ]
    expect(matchBlockRangeToBlocks([0, 0], blocks, W, H)).toHaveLength(0)
  })

  it('page 参数:作为 HighlightRect.page 透传', () => {
    const blocks = [makeBlock('内容', [0, 0, 1, 0.1], 0)]
    const hits = matchBlockRangeToBlocks([0, 0], blocks, W, H, 7)
    expect(hits[0].page).toBe(7)
  })

  it('反序区间(start>end):返回空(防御性,避免误匹配)', () => {
    const blocks = [makeBlock('内容', [0, 0, 1, 0.1], 5)]
    expect(matchBlockRangeToBlocks([10, 5] as unknown as [number, number], blocks, W, H)).toHaveLength(0)
  })

  it('非元组/长度!=2:返回空(防御性)', () => {
    const blocks = [makeBlock('内容', [0, 0, 1, 0.1], 0)]
    expect(matchBlockRangeToBlocks(null as unknown as [number, number], blocks, W, H)).toHaveLength(0)
    expect(matchBlockRangeToBlocks([5] as unknown as [number, number], blocks, W, H)).toHaveLength(0)
  })
})


describe('V8-S6 路径优先级(黑盒验证)', () => {
  /** 模拟 PdfViewer 的高亮路径选择:
   *  - 有 block_range → matchBlockRangeToBlocks(不依赖 chunk_text)
   *  - 没 block_range 但有 highlight → matchHighlightToBlocks(fallback)
   */
  function pickHits(args: {
    blockRange?: [number, number] | null
    highlight?: string
    blocks: Block[]
  }): { rects: ReturnType<typeof matchBlockRangeToBlocks>; path: 'block_range' | 'fallback' | 'none' } {
    if (args.blockRange) {
      return { rects: matchBlockRangeToBlocks(args.blockRange, args.blocks, W, H, 0), path: 'block_range' }
    }
    if (args.highlight) {
      return { rects: matchHighlightToBlocks(args.highlight, args.blocks, W, H, 0), path: 'fallback' }
    }
    return { rects: [], path: 'none' }
  }

  it('block_range 优先:即使 highlight 也存在,仍走坐标路径', () => {
    const blocks = [
      makeBlock('X', [0, 0, 1, 0.1], 0),
      makeBlock('Y', [0, 0.2, 1, 0.3], 5),
    ]
    const r = pickHits({
      blockRange: [5, 5],
      highlight: 'X',  // 即使 highlight 存在
      blocks,
    })
    expect(r.path).toBe('block_range')
    expect(r.rects).toHaveLength(1)
    // 命中的是 block_order=5 的 "Y",不是 highlight "X" 的 block_order=0
    expect(r.rects[0].y).toBe(400)
  })

  it('block_range 缺失 + highlight 存在 → 走 fallback 字符串匹配', () => {
    const blocks = [
      makeBlock('目标内容', [0, 0, 1, 0.1], 0),
      makeBlock('无关', [0, 0.2, 1, 0.3], 5),
    ]
    const r = pickHits({
      blockRange: null,
      highlight: '目标内容',
      blocks,
    })
    expect(r.path).toBe('fallback')
    expect(r.rects).toHaveLength(1)
  })

  it('block_range=null + highlight=null → 不画高亮(path=none)', () => {
    const blocks = [makeBlock('内容', [0, 0, 1, 0.1], 0)]
    const r = pickHits({ blockRange: null, blocks })
    expect(r.path).toBe('none')
    expect(r.rects).toHaveLength(0)
  })
})