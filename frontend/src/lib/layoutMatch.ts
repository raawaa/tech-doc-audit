/**
 * PDF 高亮匹配：把 URL 参数 `highlight`（chunk_text 字符串）映射到一页的
 * layout blocks 列表，产出命中 block 的 bbox_norm 像素矩形。
 *
 * 设计要点：
 * - **N3 归一化**：NFKC + casefold + 去空白 + 去中英标点。
 *   双方都必须先归一化再比，否则 NFKC 的等价表示差异（半角/全角、合字、组合字符）
 *   会让本应命中的匹配失效，详见 retrospective #4 / 2026-07-06。
 * - **T1 includes 优先**：归一化后 ``norm(block_content).includes(norm(highlight))``
 *   命中即计。这是绝大多数匹配场景（完全相等 + 加空格 + 全角字符）的兜底。
 * - **P2 LCS 兜底**：includes 未命中时跑字符级 LCS（DP, Uint16Array），阈值
 *   ``lcs/min(a,b) >= 0.85`` 且 ``min(a,b) >= 4``。低于 4 字符不跑 LCS——短串
 *   噪声比太高，已知不做 OCR 容错。
 *
 * 坐标系：``bbox_norm = [x1/W, y1/H, x2/W, y2/H]``（0-1 浮点），调用方传
 * ``pageW``/``pageH``（像素）即可。矩形坐标含 1px 内边距适配 canvas stroke，
 * 与历史实现兼容。
 *
 * 纯函数：无 DOM / pdfjs / React 运行时依赖，可独立测试。
 */

/** 一个 layout block（与服务端 ``/layout`` 返回的 blocks[] 形状对齐）。 */
export interface Block {
  block_label?: string
  block_content: string
  bbox_norm: number[]
  polygon_norm?: number[][]
  block_order?: number
}

/** 一条命中的矩形（画布像素坐标，顶原点）。 */
export interface HighlightRect {
  /** 页号（0-based，与 PageLayout.page 一致）。 */
  page: number
  /** 命中矩形左上 x（画布像素）。 */
  x: number
  /** 命中矩形左上 y（画布像素，顶原点）。 */
  y: number
  /** 命中矩形宽（画布像素）。 */
  w: number
  /** 命中矩形高（画布像素）。 */
  h: number
}

/** 不参与 LCS 兜底的最小串长（少于 4 字符噪声比太高且短到无意义）。 */
const MIN_LCS_LEN = 4
/** LCS ratio 阈值：归一化后命中长度 / min(a, b) >= 此值才算命中。 */
const LCS_RATIO_THRESHOLD = 0.85

/** 中英常见标点 + 控制字符类空白（NFKC 后还要剥这一层把"加空格"也算命中）。 */
const PUNCT_RE =
  /[\s\u3000\u2000-\u200f\u2028-\u202f\uFEFF!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~，。！？、；：（）【】「」『』《》·…—–]/g


/**
 * N3 归一化：NFKC + casefold + 去空白 + 去中英标点。
 *
 * 顺序固定：先 NFKC 再 lowercase（避免某些 Unicode 字符归一化前 casefold
 * 失效），最后剥空白与标点。
 */
export function norm(s: string): string {
  if (!s) return ''
  const nfkc = s.normalize('NFKC')
  const lower = nfkc.toLowerCase()
  return lower.replace(PUNCT_RE, '')
}


/**
 * 字符级 LCS 长度（DP, Uint16Array），返回 ``lcs(a, b)`` 的字符数。
 * 时间 / 空间 O(n*m)。调用方负责 m/n 都别太大（典型 highlight 是几十字符）。
 */
function lcsLen(a: string, b: string): number {
  const n = a.length
  const m = b.length
  if (!n || !m) return 0

  // Uint16Array：LCS 长度不会超过 65535，对 highlight < 100 字符足够；超过则降级 Number[]。
  const useUint16 = n * m <= 0xffff
  let prev: Uint16Array | number[]
  let curr: Uint16Array | number[]
  if (useUint16) {
    prev = new Uint16Array(m + 1)
    curr = new Uint16Array(m + 1)
  } else {
    prev = new Array<number>(m + 1).fill(0)
    curr = new Array<number>(m + 1).fill(0)
  }

  for (let i = 1; i <= n; i++) {
    const ai = a.charCodeAt(i - 1)
    for (let j = 1; j <= m; j++) {
      if (ai === b.charCodeAt(j - 1)) {
        ;(curr as Uint16Array)[j] = (prev as Uint16Array)[j - 1] + 1
      } else {
        const left = (curr as Uint16Array)[j - 1]
        const top = (prev as Uint16Array)[j]
        ;(curr as Uint16Array)[j] = left >= top ? left : top
      }
    }
    // swap
    const tmp = prev
    prev = curr
    curr = tmp
    if (useUint16) {
      ;(curr as Uint16Array).fill(0)
    } else {
      ;(curr as number[]).fill(0)
    }
  }
  return (prev as Uint16Array)[m]
}


/**
 * LCS ratio：``lcs / min(a.length, b.length)``，值 ∈ [0, 1]。
 */
export function lcsRatio(a: string, b: string): number {
  const na = norm(a)
  const nb = norm(b)
  if (!na.length || !nb.length) return 0
  const shorter = na.length <= nb.length ? na : nb
  const longer = na.length <= nb.length ? nb : na
  // 小串 < MIN_LCS_LEN 时直接 0（与 matchHighlightToBlocks 同步：短串不跑 LCS）
  if (shorter.length < MIN_LCS_LEN) return 0
  return lcsLen(shorter, longer) / shorter.length
}


/**
 * 把 URL 参数 ``highlight`` 映射到一页的 layout blocks 列表，产出命中 block 的
 * 画布像素矩形（顶原点）。
 *
 * 匹配优先级：
 * @param pageW    PDF 页面渲染像素宽（react-pdf <Page> 内部分辨率 = 磅 × scale）。
 * @param pageH    PDF 页面渲染像素高。
 * @param page     该页的逻辑页号（0-based，与 PageLayout.page 对齐）。默认 0，
 *                 调用方按页迭代时显式传入以让 HighlightRect.page 准确（PdfViewer
 *                 把结果按 page 索引到 matchesByPage）。
 * @returns 命中 block 的画布像素矩形（多对一：可能多个 block 命中同一 highlight）。
 */
export function matchHighlightToBlocks(
  highlight: string,
  blocks: ReadonlyArray<Block>,
  pageW: number,
  pageH: number,
  page: number = 0,
): HighlightRect[] {
  const normHighlight = norm(highlight)
  if (!normHighlight) return []

  const hits: HighlightRect[] = []
  for (const b of blocks) {
    const content = b.block_content || ''
    const normContent = norm(content)
    if (!normContent) continue

    // T1：双向 includes（block 是 highlight 子串也算，OCR 拆散场景）
    let matched =
      normContent.includes(normHighlight) ||
      normHighlight.includes(normContent)
    // P2：LCS 兜底
    if (!matched) {
      const shortLen = Math.min(normContent.length, normHighlight.length)
      if (shortLen >= MIN_LCS_LEN) {
        const ratio = normContent.length <= normHighlight.length
          ? lcsLen(normContent, normHighlight) / shortLen
          : lcsLen(normHighlight, normContent) / shortLen
        matched = ratio >= LCS_RATIO_THRESHOLD
      }
    }
    if (!matched) continue

    const bbox = b.bbox_norm
    if (!Array.isArray(bbox) || bbox.length !== 4) continue
    const x = bbox[0] * pageW
    const y = bbox[1] * pageH
    const w = (bbox[2] - bbox[0]) * pageW
    const h = (bbox[3] - bbox[1]) * pageH
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) continue
    if (w <= 0 || h <= 0) continue

    hits.push({ page, x, y, w, h })
  }
  return hits
}
