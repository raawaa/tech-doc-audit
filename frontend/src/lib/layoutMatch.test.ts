import { describe, it, expect } from 'vitest'
import {
  norm,
  lcsRatio,
  matchHighlightToBlocks,
  blockMatchesHighlight,
  type Block,
} from './layoutMatch'


describe('norm', () => {
  it('NFKC 全角数字归一为半角', () => {
    expect(norm('８00兆对讲机')).toBe('800兆对讲机')
  })

  it('lowercase', () => {
    expect(norm('ABC Def')).toBe('abcdef')
  })

  it('去空白', () => {
    expect(norm('公 司 各 应 急')).toBe('公司各应急')
  })

  it('去常见中文标点', () => {
    expect(norm('公司、各应急单位应当配置。')).toBe('公司各应急单位应当配置')
  })
  it('去常见英文标点（NFKC 后统一为半角）', () => {
    expect(norm('(800) MHz radio, OK!')).toBe('800mhzradiook')
  })

  it('全角字母 NFKC 归一为半角', () => {
    expect(norm('（ＡＢＣ）')).toBe('abc')
  })

  it('空串归一化结果为空串', () => {
    expect(norm('')).toBe('')
  })
})


describe('lcsRatio', () => {
  it('完全相等返回 1', () => {
    expect(lcsRatio('hello', 'hello')).toBe(1)
  })

  it('短串 ≥ MIN_LCS_LEN 时比 LCS', () => {
    // 4 字符是门槛
    expect(lcsRatio('abcd', 'abce')).toBeCloseTo(3 / 4, 5)
  })

  it('短串 < 4 字符返回 0（短路）', () => {
    expect(lcsRatio('abc', 'abcd')).toBe(0)
    expect(lcsRatio('a', 'bb')).toBe(0)
  })

  it('NFKC 归一化后比，8 ↔ ８ 合字（短于 4 字符仍走 includes，不经此函数）', () => {
    // 全角 ８ vs 半角 8；3 字符 < MIN_LCS_LEN，lcsRatio 短路返回 0
    // 真正的全场景合字验证在 matchHighlightToBlocks 的\"全角字符\" case 走 includes 路径
    expect(lcsRatio('８00', '800')).toBe(0)
  })

  it('OCR 单字错（讲 → 话）：lcsRatio < 1 但 > 0', () => {
    // "800兆对讲机" vs "800兆对话机"，差异 1/6 字符
    const r = lcsRatio('800兆对讲机', '800兆对话机')
    expect(r).toBeGreaterThan(0.7)
    expect(r).toBeLessThan(1)
  })

  it('两个空串返回 0', () => {
    expect(lcsRatio('', '')).toBe(0)
  })
})


describe('matchHighlightToBlocks', () => {
  // 让 pageW = 1000 / pageH = 2000，便于 bbox_norm × 尺寸直接读像素
  const W = 1000
  const H = 2000

  const makeBlock = (block_content: string, bbox: [number, number, number, number]): Block => ({
    block_label: 'text',
    block_content,
    bbox_norm: bbox,
    block_order: 0,
  })

  it('T1 完全匹配：includes 命中', () => {
    const blocks = [
      makeBlock('公司各应急保障单位应当配置', [0.1, 0.1, 0.9, 0.2]),
      makeBlock('无关文本', [0.1, 0.3, 0.9, 0.4]),
    ]
    const hits = matchHighlightToBlocks('公司各应急保障', blocks, W, H)
    expect(hits).toHaveLength(1)
    expect(hits[0].x).toBe(100)
    expect(hits[0].y).toBe(200)
    expect(hits[0].w).toBe(800)
    expect(hits[0].h).toBe(200)
  })

  it('加空格 / 标点：归一化后命中', () => {
    // 用户传的 highlight 含空格，PDF 文本无空格 → 都归一化掉
    const blocks = [
      makeBlock('公司各应急保障单位', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('公司 各应急 保障单位', blocks, W, H)).toHaveLength(1)
    // 反向：PDF 文本含标点 / 空格，用户输入干净
    expect(matchHighlightToBlocks('各应急保障', blocks, W, H)).toHaveLength(1)
  })

  it('全角字符：NFKC 归一化后命中', () => {
    const blocks = [
      makeBlock('800兆对讲机', [0.1, 0.1, 0.9, 0.2]),
    ]
    // highlight 全角 ８ → NFKC 后变半角
    const hits = matchHighlightToBlocks('８00兆对讲机', blocks, W, H)
    expect(hits).toHaveLength(1)
  })

  it('OCR 单字错（LCS 兜底）：includes miss 但 ratio >= 0.85', () => {
    // 讲 → 话，1 个字符差异；highlight 长度 6，block 长度 6，min = 6
    // LCS = 5 / 6 = 0.833... < 0.85，所以这条不会命中——但**不算 bug**，阈值就是 0.85。
    // 改用更长的差异更小的串让 LCS ratio ≥ 0.85：
    // 14 个字符的串错 1 个 → ratio = 13/14 = 0.928
    const longText = '公司各应急保障单位应当配置无线对讲设备至少两套'
    const blocks = [
      makeBlock(longText, [0.1, 0.1, 0.9, 0.2]),
    ]
    // highlight 与 block 仅一字符之差
    const typoHighlight = '公司各应急保障单位应当配置无线对话设备至少两套'  // "对讲" → "对话"
    const hits = matchHighlightToBlocks(typoHighlight, blocks, W, H)
    expect(hits).toHaveLength(1)
  })

  it('OCR 单字错（短串）：includes miss + LCS 短路（短串不跑 LCS）', () => {
    // 6 字符差异 1 → ratio 5/6 = 0.833 < 0.85，同时仍 >= 4，所以会跑 LCS，
    // 但 ratio 不达标 → 不命中。验证：短到 MIN_LCS_LEN 边界
    // 4 字符差异 1 → ratio 3/4 = 0.75, 跑 LCS 后不命中
    const blocks = [
      makeBlock('abcd', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('abce', blocks, W, H)).toHaveLength(0)
  })

  it('短串 < 4 字符：includes miss 时不跑 LCS', () => {
    // 4 字符 highlight / 4 字符 block 内容不同：includes miss → LCS 也不足以命中
    // 4 字符刚好踩 MIN_LCS_LEN 门槛；ratio 1/4 = 0.25 < 0.85
    const blocks = [
      makeBlock('wxyz', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('abcd', blocks, W, H)).toHaveLength(0)

    // 3 字符 highlight < MIN_LCS_LEN：includes miss → 直接不命中（连 LCS 都不跑）
    const blocks2 = [
      makeBlock('wxyz', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('abc', blocks2, W, H)).toHaveLength(0)
  })

  it('完全无关文本：无命中', () => {
    const blocks = [
      makeBlock('与本标准无关的其他规范', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('800兆对讲机', blocks, W, H)).toHaveLength(0)
  })

  it('空 highlight 不返回任何命中', () => {
    const blocks = [
      makeBlock('公司各应急保障', [0.1, 0.1, 0.9, 0.2]),
    ]
    expect(matchHighlightToBlocks('', blocks, W, H)).toHaveLength(0)
    // 归一化后也是空串的也不命中
    expect(matchHighlightToBlocks('   ', blocks, W, H)).toHaveLength(0)
  })

  it('block.bbox_norm 长度不是 4：跳过该 block（仍算"匹配但不画"）', () => {
    const blocks = [
      { block_label: 'text', block_content: '公司各应急保障', bbox_norm: [0.1, 0.2, 0.9] as unknown as number[] },
      makeBlock('公司各应急保障', [0.1, 0.1, 0.9, 0.2]),
    ]
    const hits = matchHighlightToBlocks('公司各应急保障', blocks, W, H)
    expect(hits).toHaveLength(1)
  })

  it('多个 block 同时命中：全部入 hits', () => {
    // 同一 highlight 在两块文本里都出现（OCR 把同一段重复印了 / 多页冗余）
    const blocks = [
      makeBlock('公司各应急保障单位应当配置', [0.1, 0.1, 0.9, 0.2]),
      makeBlock('公司各应急保障单位应当配置', [0.1, 0.3, 0.9, 0.4]),
    ]
    expect(matchHighlightToBlocks('公司各应急保障', blocks, W, H)).toHaveLength(2)
  })

  it('block 是 highlight 子串（OCR 拆散）：双向 includes 命中', () => {
    // 例如 highlight = "公司各应急保障单位应当配置无线对讲设备至少两套"
    // 但 PDF OCR 后拆成两段："公司各应急保障单位应当配置" + "无线对讲设备至少两套"
    // 第一段是 highlight 的真子串 → 双向 includes 命中
    const blocks = [
      makeBlock('公司各应急保障单位应当配置', [0.1, 0.1, 0.9, 0.2]),
      makeBlock('无关文本', [0.1, 0.3, 0.9, 0.4]),
    ]
    const hits = matchHighlightToBlocks(
      '公司各应急保障单位应当配置无线对讲设备至少两套',
      blocks,
      W, H,
    )
    expect(hits).toHaveLength(1)
  })

  // ── Bug B:highlight 字符在 content 里散开不应误命中 ───────────────────────
  //
  // 之前 ratio = lcs / min(content, highlight)：content 长且 highlight 短时，
  // content 里散落 highlight 字符会被 LCS 当成 1.0 命中。典型场景：
  //   highlight = "应急救援指挥中心" (8 字符)
  //   content = "...应急救援保障能力...消防急救保障部应及时向公司运行指挥中心报告..."
  // LCS 把 8 个字符全部按序匹配到 content 里，old ratio = 8/8 = 1.0 → 命中。
  // 改成 lcs / max 后 ratio = 8/len(content) ≈ 0.12 → 不命中。
  it('highlight 字符散落长 content 不应误命中（LCS ratio 用 max 而非 min）', () => {
    const blocks = [
      // 真实 PDF 里 page 13 的一个段落：包含 "应急救援" + "指挥中心"，
      // 中间隔了 25+ 个字符。LCS 按序数得 8/8 → 旧逻辑误判。
      makeBlock(
        '公司消防、医疗救护相关设备物资发生报废、故障、检修等情况，造成机场应急救援保障能力降低的，消防急救保障部应及时向公司运行指挥中心报告。',
        [0.1, 0.3, 0.9, 0.4],
      ),
      // 真命中的对照块——"应急救援指挥中心" 整段在这
      makeBlock('第八条 应急救援指挥中心', [0.1, 0.1, 0.9, 0.2]),
    ]
    const hits = matchHighlightToBlocks('应急救援指挥中心', blocks, W, H)
    // 只应命中第 2 个 block（包含真短语的那条），不应命中第 1 个（散落命中）。
    expect(hits).toHaveLength(1)
    expect(hits[0].y).toBe(200) // 第 2 个 block 的 y = 0.1 * H = 200
  })
})


describe('blockMatchesHighlight', () => {
  const makeBlock = (block_content: string): Block => ({
    block_label: 'text',
    block_content,
    bbox_norm: [0.1, 0.1, 0.9, 0.2],  // 对 predicate 而言 bbox 无关
    block_order: 0,
  })

  it('T1 完全匹配:includes 命中', () => {
    expect(blockMatchesHighlight(makeBlock('公司各应急保障单位应当配置'), '公司各应急保障')).toBe(true)
  })

  it('双向 includes(block 是 highlight 子串)命中:OCR 拆散场景', () => {
    expect(
      blockMatchesHighlight(
        makeBlock('公司各应急保障单位应当配置'),
        '公司各应急保障单位应当配置无线对讲设备至少两套',
      ),
    ).toBe(true)
  })

  it('加空格 / 标点:归一化后命中', () => {
    expect(blockMatchesHighlight(makeBlock('公司各应急保障单位'), '公司 各应急 保障单位')).toBe(true)
  })

  it('全角字符:NFKC 后命中', () => {
    expect(blockMatchesHighlight(makeBlock('800兆对讲机'), '８00兆对讲机')).toBe(true)
  })

  it('完全无关文本:不命中', () => {
    expect(blockMatchesHighlight(makeBlock('与本标准无关的其他规范'), '800兆对讲机')).toBe(false)
  })

  it('空 highlight / 空 content:不命中', () => {
    expect(blockMatchesHighlight(makeBlock('公司各应急保障'), '')).toBe(false)
    expect(blockMatchesHighlight(makeBlock('公司各应急保障'), '   ')).toBe(false)
    expect(blockMatchesHighlight(makeBlock(''), '800兆对讲机')).toBe(false)
  })

  it('OCR 长串 1 字差异:走 LCS ratio >= 0.85 命中', () => {
    // 与 matchHighlightToBlocks 同型:14 字符错 1 → ratio 13/14 ≈ 0.928
    expect(
      blockMatchesHighlight(
        makeBlock('公司各应急保障单位应当配置无线对讲设备至少两套'),
        '公司各应急保障单位应当配置无线对话设备至少两套',
      ),
    ).toBe(true)
  })

  it('短串差异不达阈值:LCS miss', () => {
    // 4 字符差异 1 → LCS ratio 3/4 = 0.75 < 0.85
    expect(blockMatchesHighlight(makeBlock('abcd'), 'abce')).toBe(false)
  })

  it('highlight 字符散落长 content:不误命中(LCS ratio 用 max 而非 min)', () => {
    // 与 matchHighlightToBlocks 的"散落不应误命中" case 同型
    const scattered = '公司消防、医疗救护相关设备物资发生报废、故障、检修等情况,造成机场应急救援保障能力降低的,消防急救保障部应及时向公司运行指挥中心报告。'
    expect(blockMatchesHighlight(makeBlock(scattered), '应急救援指挥中心')).toBe(false)
    expect(blockMatchesHighlight(makeBlock('第八条 应急救援指挥中心'), '应急救援指挥中心')).toBe(true)
  })
})
