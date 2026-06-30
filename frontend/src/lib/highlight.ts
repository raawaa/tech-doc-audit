/**
 * PDF 高亮：纯函数，解耦"匹配层"与"绘制层"。
 *
 * 匹配层用 pdfjs 的 PDFDocumentProxy.getTextContent() 产出文本块，
 * 经 computePageMatches() 换算为 **画布像素（已含渲染缩放）** 的命中矩形；
 * 绘制层由 paintHighlight() 在某页 canvas 上画出黄色高亮。
 *
 * 坐标系：pdfjs 文本 transform 为 PDF 磅（底原点）。react-pdf <Page> 把 canvas
 * 内部分辨率设为 `磅 × scale × devicePixelRatio`（默认 scale=1），故匹配层需乘以
 * 该有效缩放，把磅坐标一次性烘焙进 matches；绘制层只用 canvas.height 做 y 翻转。
 *
 * 两层各自可独立测试：本文件无任何 DOM / React / pdfjs 运行时依赖。
 */

/** pdfjs 文本块的最小子集（仅取我们需要的字段）。 */
export interface PdfTextItem {
  str: string
  /** pdfjs transform [a, b, c, d, e, f]；a≈字宽，e=x，f=y(底原点)。 */
  transform: number[]
}

/**
 * 一条命中的矩形，**画布像素坐标（已烘焙渲染缩放）**。
 * y 仍以底原点表示（bottomY = 距画布底距离），由绘制层用 canvas.height 翻转。
 */
export interface HighlightMatch {
  /** 画布 x（左上角，已减内边距）。 */
  x: number
  /** 文本基线距画布底部的像素距离（= transform[5] × scale）。 */
  bottomY: number
  /** 矩形宽度（已加内边距）。 */
  width: number
  /** 矩形高度（画布像素，固定）。 */
  height: number
}

/** 过短（<2 字符）的搜索词不参与匹配。 */
const MIN_TERM_LEN = 2
/** 命中矩形相对文本基线的上偏移与尺寸（画布像素，与历史实现一致）。 */
const ASCENT_OFFSET = 14
const RECT_HEIGHT = 18
const PAD_X = 1

/**
 * 将 pdfjs 一页文本块中、命中搜索词的项换算为画布像素命中矩形。
 *
 * 纯函数：不触碰 DOM。把渲染缩放 `scale`（= react-pdf scale × devicePixelRatio）
 * 一次性烘焙进坐标；y 的 canvas.height 翻转推迟到 paintHighlight()。
 *
 * @param items pdfjs getTextContent().items（取 str / transform）
 * @param terms 搜索词（空格分隔）；<2 字符的词被忽略；项命中任一词即计为命中
 * @param scale 渲染缩放（react-pdf scale × devicePixelRatio）
 */
export function computePageMatches(
  items: ReadonlyArray<PdfTextItem>,
  terms: ReadonlyArray<string>,
  scale: number,
): HighlightMatch[] {
  const effectiveTerms = terms.filter(t => t.length >= MIN_TERM_LEN)
  if (effectiveTerms.length === 0) return []

  const matches: HighlightMatch[] = []
  const seen = new Set<number>() // 同一 item 命中多词只计一次

  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    const str = item.str || ''
    if (!str) continue

    let hit = false
    for (const term of effectiveTerms) {
      if (str.includes(term)) {
        hit = true
        break
      }
    }
    if (!hit) continue
    if (seen.has(i)) continue
    seen.add(i)

    const tx = item.transform
    const charWidth = tx[0] || 8
    matches.push({
      x: tx[4] * scale - PAD_X,
      bottomY: tx[5] * scale,
      width: str.length * charWidth * scale * 0.6 + PAD_X * 2,
      height: RECT_HEIGHT,
    })
  }

  return matches
}

/**
 * 在给定 canvas 上绘制高亮矩形。
 *
 * 读取 canvas.height 做最终的 y 翻转、读取 2d context 画黄色矩形。
 * matches 已含渲染缩放（由 computePageMatches 烘焙），故本函数无需 scale。
 * canvas 为 null 或无匹配时为空操作。
 *
 * 同时服务两条路径：页渲染完成时绘制、匹配层产出后对"已挂载但尚未高亮"的页补画，
 * 以消除"匹配晚于渲染则高亮不出现"的竞态。
 *
 * @param canvas react-pdf <Page> 注入的 canvas（通过 canvasRef 拿到）
 * @param _pageNum 仅作语义标注（matches 已是某页专属），当前未使用
 * @param matches computePageMatches() 对该页的产出（画布像素坐标）
 */
export function paintHighlight(
  canvas: HTMLCanvasElement | null,
  _pageNum: number,
  matches: ReadonlyArray<HighlightMatch>,
): void {
  if (!canvas || matches.length === 0) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
  for (const m of matches) {
    const y = canvas.height - m.bottomY - ASCENT_OFFSET
    ctx.fillRect(m.x, y, m.width, m.height)
  }
}
