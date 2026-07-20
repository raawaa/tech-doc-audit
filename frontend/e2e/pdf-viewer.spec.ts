import { test, expect, type Page } from '@playwright/test'
import path from 'path'
import { fileURLToPath } from 'url'

// 真实(小)PDF fixture。drop-in 的 annotation 状态只有在 pdfium 真正加载了
// 有效 PDF、fire onLayoutReady 后才初始化 —— headless 时代的 `highlight-rect`
// 是纯 React DOM overlay(不依赖 PDF 加载),故当年用占位 `%PDF-1.4 dummy`
// 就够;annotation 断言必须喂真 PDF。round-trip 的 rect 期望值仍来自 mock
// layout 的 page dims(1000×2000),与该 PDF 的真实尺寸无关。
const FIXTURE_PDF = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  'fixtures',
  'sample.pdf',
)

/**
 * PDF viewer 端到端测试(V9 PRD #68 — drop-in 替换后)。
 *
 * 生产 `/pdf-viewer/:docId` 现基于 `@embedpdf/react-pdf-viewer` drop-in。
 * 高亮不再是 headless 的 `[data-testid="highlight-rect"]` 百分比 div,而是
 * drop-in annotation plugin 的 annotation —— 通过 `window.__pdfViewerRegistry`
 * 句柄(仅 `import.meta.env.DEV` 下暴露,playwright 跑 `npm run dev` 故可用)
 * 拿 `getAnnotations()` 断言。#66 spike 的 `pdf-viewer-dropin.spec.ts` 已合并到此。
 *
 * 覆盖入口:
 * 1. 直接打开 /pdf-viewer/:docId —— "PDF 没显示" 回归
 * 2. 从审核结果页点 standard 链接跳过去 —— 用户实际路径
 * 3. URL 契约 ?page= / ?block_range= / ?highlight= 行为(annotation 断言)
 * 4. round-trip:annotation 落在 PDF 用户空间 + color/opacity/commitState
 */

const PDF_DOC_ID = '01KW10F4SD4BZG6SQFNQ42JDTH'
const AUDIT_DOC_ID = '01KVY36VHQGD6S3SBH5JWRF7ZS'
const AUDIT_TASK_ID = '01KWK9SR726Q57SXK9V7NVC9GB'
const REAL_DOC_ID = '01KW1QXZ5AKBGK34BDJRV1X4JZ'

// ── drop-in registry 句柄:annotation 断言渠道 ─────────────────────────────

async function waitForViewerRegistry(page: Page, timeoutMs = 30_000) {
  await expect
    .poll(
      async () =>
        (await page.evaluate(
          () => !!(window as unknown as { __pdfViewerRegistry?: unknown })
            .__pdfViewerRegistry,
        )) === true,
      { timeout: timeoutMs, intervals: [500, 1000, 2000] },
    )
    .toBe(true)
}

async function getAnnotationCount(page: Page, docId: string): Promise<number> {
  return (await page.evaluate(async (id) => {
    const reg = (window as unknown as {
      __pdfViewerRegistry?: Promise<{
        getPlugin: (p: string) => {
          provides?: () => {
            forDocument: (d: string) => {
              getAnnotations: (opts?: { pageIndex?: number }) => unknown[]
            }
          } | null
        } | null
      }>
    }).__pdfViewerRegistry
    if (!reg) return -1
    const r = await reg
    const ann = r.getPlugin('annotation')?.provides?.()
    if (!ann) return -2
    // annotation state 在 importAnnotations 跑之前不存在,getAnnotations() 会抛;
    // 返回哨兵让 expect.poll 继续重试,直到 importAnnotations 初始化了状态。
    try {
      return ann.forDocument(id).getAnnotations().length
    } catch {
      return -3
    }
  }, docId)) as number
}

async function getAnnotationPageIndexes(page: Page, docId: string): Promise<number[]> {
  return (await page.evaluate(async (id) => {
    const reg = (window as unknown as {
      __pdfViewerRegistry?: Promise<{
        getPlugin: (p: string) => {
          provides?: () => {
            forDocument: (d: string) => {
              getAnnotations: () => Array<{ object: { pageIndex: number } }>
            }
          } | null
        } | null
      }>
    }).__pdfViewerRegistry
    if (!reg) return []
    const r = await reg
    const ann = r.getPlugin('annotation')?.provides?.()
    if (!ann) return []
    try {
      return ann.forDocument(id).getAnnotations().map(a => a.object.pageIndex)
    } catch {
      return []
    }
  }, docId)) as number[]
}

async function getFirstAnnotationPayload(page: Page, docId: string): Promise<{
  pageIndex: number
  strokeColor: string | null
  color: string | null
  opacity: number
  rectOriginX: number
  rectOriginY: number
  rectWidth: number
  rectHeight: number
  segmentRects: number
  commitState: string
} | null> {
  return (await page.evaluate(async (id) => {
    const reg = (window as unknown as {
      __pdfViewerRegistry?: Promise<{
        getPlugin: (p: string) => {
          provides?: () => {
            forDocument: (d: string) => {
              getAnnotations: () => Array<{
                commitState: string
                object: {
                  pageIndex: number
                  strokeColor?: string
                  color?: string
                  opacity: number
                  rect: {
                    origin: { x: number; y: number }
                    size: { width: number; height: number }
                  }
                  segmentRects?: unknown[]
                }
              }>
            }
          } | null
        } | null
      }>
    }).__pdfViewerRegistry
    if (!reg) return null
    const r = await reg
    const ann = r.getPlugin('annotation')?.provides?.()
    if (!ann) return null
    let all
    try {
      all = ann.forDocument(id).getAnnotations()
    } catch {
      return null
    }
    if (all.length === 0) return null
    const obj = all[0].object
    return {
      pageIndex: obj.pageIndex,
      strokeColor: obj.strokeColor ?? null,
      color: obj.color ?? null,
      opacity: obj.opacity,
      rectOriginX: obj.rect.origin.x,
      rectOriginY: obj.rect.origin.y,
      rectWidth: obj.rect.size.width,
      rectHeight: obj.rect.size.height,
      segmentRects: obj.segmentRects?.length ?? 0,
      commitState: all[0].commitState,
    }
  }, docId)) as Awaited<ReturnType<typeof getFirstAnnotationPayload>>
}

// ── mock helpers ──────────────────────────────────────────────────────────

async function mockLayoutWith(page: Page, layout: object) {
  await page.route('**/api/v1/kb-documents/*/layout', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(layout),
    }),
  )
}

async function mockMeta(
  page: Page,
  overrides: Partial<{ name: string; page_count: number; file_type: string }> = {},
) {
  await page.route('**/api/v1/kb-documents/*', route => {
    const url = route.request().url()
    if (/\/api\/v1\/kb-documents\/[^/]+$/.test(url)) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'mock',
          name: overrides.name ?? 'mock.pdf',
          original_name: 'mock.pdf',
          file_type: overrides.file_type ?? 'pdf',
          page_count: overrides.page_count ?? 1,
          kb_id: 'mock_kb',
        }),
      })
    }
    return route.continue()
  })
}

async function mockLayoutNotFound(page: Page) {
  await page.route('**/api/v1/kb-documents/*/layout', route =>
    route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '该文档未解析' }),
    }),
  )
}

async function mockPdfFile(page: Page) {
  await page.route('**/api/v1/kb-documents/*/file', route =>
    route.fulfill({
      status: 200,
      path: FIXTURE_PDF,
      contentType: 'application/pdf',
    }),
  )
}

function makeLayout(blocks: Array<{ content: string; y1: number; y2: number; order: number }>) {
  return {
    has_layout: true,
    layout: [{
      page: 0,
      width: 1000,
      height: 2000,
      blocks: blocks.map(b => ({
        block_label: 'text',
        block_content: b.content,
        bbox_norm: [0.05, b.y1, 0.95, b.y2],
        polygon_norm: [],
        block_order: b.order,
      })),
    }],
  }
}

test.describe('PDF viewer (embedpdf drop-in)', () => {
  test('直接打开 /pdf-viewer/:docId 时 [data-testid="pdf-viewer"] 在视口内', async ({ page }) => {
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=&clause=&highlight=GB%2050016`)
    await expect(page.getByTestId('pdf-viewer')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('pdf-viewer-container')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
  })

  test('从审核结果页点标准链接能跳转到 PDF viewer 并渲染', async ({ page }) => {
    await page.goto(`/audit/${AUDIT_DOC_ID}/result/${AUDIT_TASK_ID}`)
    const stdLink = page.locator('a[href*="/pdf-viewer/"]').first()
    await expect(stdLink).toBeVisible({ timeout: 15_000 })
    const [popup] = await Promise.all([
      page.waitForEvent('popup'),
      stdLink.click(),
    ])
    await popup.waitForLoadState('domcontentloaded')
    expect(popup.url()).toContain(`/pdf-viewer/${PDF_DOC_ID}`)
    await expect(popup.getByTestId('pdf-viewer')).toBeVisible({ timeout: 30_000 })
    await expect(popup.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
  })

  // ── URL 契约:block_range → annotation 一一对应 ───────────────────────

  test('合法 doc + block_range 参数:layout 命中 → annotation 数 = 命中 block 数', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'GB 50016 是一个建筑防火设计规范', y1: 0.05, y2: 0.10, order: 0 },
      { content: 'GB 50016 章节 1.0.1', y1: 0.15, y2: 0.20, order: 1 },
      { content: 'GB 50016 章节 1.0.2', y1: 0.25, y2: 0.30, order: 2 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=${encodeURIComponent('0,2')}`,
    )
    await waitForViewerRegistry(page)
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(3)
  })

  test('合法 doc + highlight 字符串参数:layout 命中 → annotation 渲染', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'GB 50016 是一个建筑防火设计规范', y1: 0.05, y2: 0.10, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('GB 50016')}`,
    )
    await waitForViewerRegistry(page)
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(1)
  })

  test('未 reparse doc + 带 highlight:E1 fallback UI 出现', async ({ page }) => {
    await mockLayoutNotFound(page)
    await mockMeta(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('800兆对讲机')}`,
    )
    await expect(page.getByText(/该文档未解析/)).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: /重新解析/ })).toBeVisible()
  })

  // ── DOCX/MD fallback 路径 ─────────────────────────────────────────────

  test('DOCX doc:text-fallback 路径渲染 + header 显示共 N 页', async ({ page }) => {
    await page.route('**/api/v1/kb-documents/*/page/*', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ text: '这是 DOCX 第 1 页正文内容', total_pages: 5 }),
      }),
    )
    await mockMeta(page, { file_type: 'docx', page_count: 5, name: 'report.docx' })
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=2`)
    await expect(page.getByTestId('text-fallback')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/第 2 \/ 5 页/)).toBeVisible()
  })

  // ── scrollbar 稳定性回归(PRD #62 user story 7,drop-in 上重新验证)──────

  test('拖到文档中部停留 5s:scrollTop / scrollHeight 唯一值计数 ≤ 3', async ({ page }) => {
    await mockMeta(page, { name: 'scrollbar-test.pdf', page_count: 30 })
    await mockPdfFile(page)

    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=1`)
    await expect(page.getByTestId('pdf-viewer')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    // 让 embedpdf 完成 scroll layout 稳定
    await page.waitForTimeout(3_000)

    const stats = await page.evaluate(async () => {
      const root = document.querySelector('[data-testid="pdf-viewer"]')
      if (!root) return null
      const all = root.querySelectorAll('*')
      let scroller: HTMLElement | null = null
      for (const el of all) {
        const e = el as HTMLElement
        if (e.scrollHeight > e.clientHeight + 100) { scroller = e; break }
      }
      if (!scroller) return null
      scroller.scrollTop = scroller.scrollHeight / 2
      const tops = new Set<number>()
      const heights = new Set<number>()
      const start = performance.now()
      while (performance.now() - start < 5_000) {
        tops.add(scroller.scrollTop)
        heights.add(scroller.scrollHeight)
        await new Promise(r => setTimeout(r, 100))
      }
      return { tops: tops.size, heights: heights.size }
    })

    expect(stats).not.toBeNull()
    // 原始 bug: react-virtuoso 重测量 → scrollTop/scrollHeight 抖动 14/16 个值
    // 修复后 embedpdf 一次 layout commit,只有 1 个值。给阈值 ≤ 3 容错。
    expect(stats!.tops).toBeLessThanOrEqual(3)
    expect(stats!.heights).toBeLessThanOrEqual(3)
  })

  // ── URL 契约 — auto-jump-to-first-hit(真实 doc + 后端)──────────────────

  test('?page=1&highlight=<非首页条款名>:auto-jump 到首匹配页', async ({ page }) => {
    await page.goto(
      `/pdf-viewer/${REAL_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`,
    )
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })

    // 等 embedpdf onLayoutReady 触发 + scrollToPage 完成
    await expect.poll(async () => {
      const txt = await page.getByTestId('page-counter').textContent()
      const m = txt?.match(/^(\d+)\s*\/\s*(\d+)/)
      return m ? Number(m[1]) : -1
    }, { timeout: 30_000, intervals: [500, 1000, 2000] }).toBe(4)

    const txt = await page.getByTestId('page-counter').textContent()
    expect(txt).toMatch(/^4\s*\//)
  })

  // ── URL 契约 — off-by-one 回归(annotation pageIndex based)─────────────

  test('?page=1&highlight=<非首页条款名>:annotation 落在正确 pageIndex(3 而非 2)', async ({ page }) => {
    await page.goto(
      `/pdf-viewer/${REAL_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`,
    )
    await waitForViewerRegistry(page)

    await expect.poll(
      async () => (await getAnnotationPageIndexes(page, REAL_DOC_ID)).length,
      { timeout: 30_000, intervals: [500, 1000, 2000] },
    ).toBeGreaterThan(0)

    // annotation 必须落在 pageIndex 3(0-based),不能落在 2。Bug A:0/1-based 错位。
    const pages = await getAnnotationPageIndexes(page, REAL_DOC_ID)
    expect(pages).toContain(3)
    expect(pages).not.toContain(2)
  })

  // ── URL 契约 — 非首页 block_range(PRD #72 回归)───────────────────────
  //   旧实现 getEffectiveDpr 只查 page 1 metric + page 1 width,scrollToPage(N≥2)
  //   后 page 1 不在 viewport,find 返回 undefined → dpr fallback 1 → annotation
  //   rect 不被除 effectiveDPR → X/Y/尺寸翻倍。修后用首个可见页 metric + 该页
  //   width,任意页都正确。这条断言不依赖 pdfium 渲染(registry 的 annotation
  //   state 是 JS 层可直接读到的),本地 headless 跑得起来。视觉 rect 像素
  //   断言需要 pdfium-capable 浏览器,留 CI。

  test('?page=N(N≥2)&block_range=…:annotation 落在正确 pageIndex(11,非首页 bug 回归)', async ({ page }) => {
    await page.goto(
      `/pdf-viewer/${REAL_DOC_ID}?page=12&block_range=${encodeURIComponent('8,8')}`,
    )
    await waitForViewerRegistry(page)

    await expect.poll(
      async () => (await getAnnotationPageIndexes(page, REAL_DOC_ID)).length,
      { timeout: 30_000, intervals: [500, 1000, 2000] },
    ).toBeGreaterThan(0)

    // block_order=8 应在 0-based page 11(1-based page 12),不能落在 10。
    const pages = await getAnnotationPageIndexes(page, REAL_DOC_ID)
    expect(pages).toContain(11)
    expect(pages).not.toContain(10)
  })

  // ── block_range 多 block / 单 block ────────────────────────────────────

  test('block_range=3,3 单 block:1 个 annotation', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.05, y2: 0.10, order: 0 },
      { content: 'block 1', y1: 0.15, y2: 0.20, order: 1 },
      { content: 'block 2', y1: 0.25, y2: 0.30, order: 2 },
      { content: 'block 3', y1: 0.35, y2: 0.40, order: 3 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=${encodeURIComponent('3,3')}`,
    )
    await waitForViewerRegistry(page)
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(1)
  })

  test('block_range=2,5 多 block:4 个 annotation(每 block 一个)', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.05, y2: 0.10, order: 0 },
      { content: 'block 1', y1: 0.15, y2: 0.20, order: 1 },
      { content: 'block 2', y1: 0.25, y2: 0.30, order: 2 },
      { content: 'block 3', y1: 0.35, y2: 0.40, order: 3 },
      { content: 'block 4', y1: 0.45, y2: 0.50, order: 4 },
      { content: 'block 5', y1: 0.55, y2: 0.60, order: 5 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=${encodeURIComponent('2,5')}`,
    )
    await waitForViewerRegistry(page)
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(4)
  })

  // ── block_range 未命中 → fallback 到 highlight ─────────────────────────

  test('block_range 区间无 blocks 命中 + 同 URL 带 highlight:fallback 到 highlight 全页扫描', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'fallback text 在这里', y1: 0.05, y2: 0.10, order: 0 },
      { content: '另一个 block', y1: 0.15, y2: 0.20, order: 1 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1` +
      `&block_range=${encodeURIComponent('5,5')}` +
      `&highlight=${encodeURIComponent('fallback text')}`,
    )
    await waitForViewerRegistry(page)
    // block_range 没命中,但 highlight 命中 → 应有 1 个 annotation
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(1)
  })

  // ── legacy ?highlight= 兼容(无 block_range)─────────────────────────

  test('旧 ?highlight= 无 block_range:仍能渲染 annotation', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: '应急救援指挥中心是核心', y1: 0.05, y2: 0.10, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`,
    )
    await waitForViewerRegistry(page)
    await expect.poll(() => getAnnotationCount(page, PDF_DOC_ID), {
      timeout: 30_000, intervals: [500, 1000, 2000],
    }).toBe(1)
  })

  // ── round-trip:annotation 在 PDF 用户空间(左下原点,Y-up)+ 视觉属性 ──
  //   (#66 spike 迁移过来:makeLayout pageW=1000/pageH=2000,bbox [0.05,0.1,0.95,0.2]
  //    → 期望 rect origin (50, 1600) size (900, 200))

  test('round-trip:rect 是 PDF 用户空间,strokeColor / opacity / segmentRects / commitState 与构造值一致', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.1, y2: 0.2, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=${encodeURIComponent('0,0')}`,
    )
    await waitForViewerRegistry(page)

    const payload = await getFirstAnnotationPayload(page, PDF_DOC_ID)
    expect(payload).not.toBeNull()
    expect(payload!.pageIndex).toBe(0)
    // Y-flip:页面顶部 block 应得到高 PDF-y(左下原点里"y 越大越靠上")
    expect(payload!.rectOriginY).toBeCloseTo(1600, 0)
    expect(payload!.rectOriginX).toBeCloseTo(50, 0)
    expect(payload!.rectWidth).toBeCloseTo(900, 0)
    expect(payload!.rectHeight).toBeCloseTo(200, 0)
    expect(payload!.segmentRects).toBe(1)
    // opacity & strokeColor:允许 'color' deprecated 字段作为兜底
    const color = payload!.strokeColor ?? payload!.color
    expect(color).toBe('#FFFF00')
    expect(payload!.opacity).toBeCloseTo(0.4, 5)
    // contract §3 step 9 + §5.3 + issue #76 acceptance:regression lock
    // 显式 commit() + await 后,commitState 必须落到 'synced'。commi­t
    // 自身 async,等 pdfium createPageAnnotation resolve 后才 dispatch
    // commitPendingChanges。用 expect.poll 等到状态翻过去。
    await expect
      .poll(
        async () =>
          (await getFirstAnnotationPayload(page, PDF_DOC_ID))?.commitState,
        { timeout: 5_000, intervals: [50, 100, 200, 500] },
      )
      .toBe('synced')
  })

  // ── header 跳页输入框 + Enter(user story 12)────────────────────────────
  //   占位 PDF 下 embedpdf totalPages 恒 0,counter 走 fallback `${targetPage}/...`;
  //   Enter 同步 URL 的 page → targetPage=5 → counter 显示 "5 / 10"。

  test('header page-jump-input + Enter:跳到指定页', async ({ page }) => {
    await mockMeta(page, { name: 'jump-test.pdf', page_count: 10 })
    await mockPdfFile(page)
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=1`)
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })

    const input = page.getByTestId('page-jump-input')
    await input.fill('5')
    await input.press('Enter')

    await expect.poll(async () => {
      const txt = await page.getByTestId('page-counter').textContent()
      const m = txt?.match(/^(\d+)\s*\/\s*(\d+)/)
      return m ? Number(m[1]) : -1
    }, { timeout: 10_000, intervals: [200, 500] }).toBe(5)
  })

  // ── #71 — 外层页面 scrollbar 不出现(PDF 模式)─────────────────────────
  //   修复前:<main class="flex-1 overflow-y-auto"> + PdfViewer 根容器
  //   min-h-screen → 视口外额外多出 100vh → 外层出现 scroll bar,
  //   同时 embedpdf viewer 也内嵌 scroll → 双层。
  //   修复后:PdfViewer 在 PDF 模式下把 <main> 切到 overflow-hidden。

  test('PDF 模式:<main> overflowY === hidden(无外层 scrollbar)', async ({ page }) => {
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=1`)
    await expect(page.getByTestId('pdf-viewer-container')).toBeVisible({
      timeout: 30_000,
    })

    // 给 ScrollMode context 时间把 'hidden' 推到 <main>
    await expect
      .poll(
        async () =>
          await page.evaluate(
            () =>
              window
                .getComputedStyle(document.querySelector('main') as Element)
                .overflowY,
          ),
        { timeout: 10_000, intervals: [100, 200, 500] },
      )
      .toBe('hidden')
  })

  //   DOCX/MD 行为不变:外层 <main> 仍可滚(min-h-screen 根容器撑出来的高度)

  test('DOCX text-fallback:<main> overflowY === auto(行为不变)', async ({ page }) => {
    await page.route('**/api/v1/kb-documents/*/page/*', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ text: '这是 DOCX 第 1 页正文内容', total_pages: 5 }),
      }),
    )
    await mockMeta(page, { file_type: 'docx', page_count: 5, name: 'report.docx' })
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=2`)
    await expect(page.getByTestId('text-fallback')).toBeVisible({ timeout: 10_000 })

    await expect
      .poll(
        async () =>
          await page.evaluate(
            () =>
              window
                .getComputedStyle(document.querySelector('main') as Element)
                .overflowY,
          ),
        { timeout: 5_000, intervals: [100, 200] },
      )
      .toBe('auto')
  })

  // ── reload-stable regression(issue #77)────────────────────────
  //   锁住 #76 修的「URL → 高亮恢复」在 refresh 路径下的稳定性,阻止 future
  //   regression。必须真 PDF fixture(REAL_DOC_ID),不可 mock layout — e2e
  //   覆盖的是真实 embedpdf 时序,不是 mock 时序(Candidate C 的 race 只在
  //   真实 pdfium 渲染 + viewport scroll 真实发生时出现)。N=10 reload 以
  //   稳定捕获间歇失败(对应 diagnose 的 repro 频率),单 test +30s 内
  //   完成;issue acceptance 的 50 reload 走 CI 复核。
  //   两条用例:block_range 坐标高亮路径 + highlight auto-jump 路径,spec
  //   §1.2/§1.3 各自锁一行。

  async function waitForAnnotationAfterReload(
    page: Page,
    docId: string,
    expectedPageIndex: number,
    timeoutMs = 10_000,
  ) {
    // 等 count > 0 — reload 后 annotation 重新进入状态窗口
    await expect
      .poll(
        async () => (await getAnnotationPageIndexes(page, docId)).length,
        { timeout: timeoutMs, intervals: [100, 200, 500] },
      )
      .toBeGreaterThan(0)
    // 再确认 pageIndex 命中(避免 count > 0 但落到错页 — 那是另一种
    // regression,先于 #76 的"完全消失"暴露)
    const pages = await getAnnotationPageIndexes(page, docId)
    expect(pages).toContain(expectedPageIndex)
  }

  // 循环 reload N 次,每轮重等 registry + annotation 命中。
  // 初始 goto + waitForViewerRegistry 由 caller 负责;此 helper 专门
  // 跑 N 次 page.reload(),不做 "首轮 no-op" 之类的折叠 — N 就是
  // 真实 reload 次数,跟 test name / issue acceptance ("≥ 10 次 reload")
  // 一一对应,避免 review #1 的命名漂移。
  async function reloadN(
    page: Page,
    docId: string,
    expectedPageIndex: number,
    nReloads: number,
  ) {
    for (let i = 0; i < nReloads; i++) {
      await page.reload()
      // reload 后 window.__pdfViewerRegistry 重置为 undefined,等它
      // 重新出现(DEV only 暴露,playwright 跑 `npm run dev` 故可用)
      await waitForViewerRegistry(page)
      await waitForAnnotationAfterReload(page, docId, expectedPageIndex)
    }
  }

  const RELOAD_N = 10  // ≥ 10 per #77 test plan;issue acceptance 50 走 CI 复核

  test(`reload-stable: ?page=11&block_range=8,8 在 ${RELOAD_N} 次 reload 内都命中 pageIndex=10`, async ({ page }) => {
    const url = `/pdf-viewer/${REAL_DOC_ID}?page=11&block_range=${encodeURIComponent('8,8')}`
    await page.goto(url)
    await waitForViewerRegistry(page)
    // 初始 navigation 之后,再跑 RELOAD_N 次 page.reload()
    await waitForAnnotationAfterReload(page, REAL_DOC_ID, 10)
    await reloadN(page, REAL_DOC_ID, 10, RELOAD_N)
  })

  test(`reload-stable: ?page=1&highlight=应急救援指挥中心 在 ${RELOAD_N} 次 reload 内都 auto-jump + 命中 pageIndex=3`, async ({ page }) => {
    const url = `/pdf-viewer/${REAL_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`
    await page.goto(url)
    await waitForViewerRegistry(page)
    // auto-jump 路径依赖 firstHitPage0 计算 + scrollToPage;reload 后
    // 这条链重新跑,期待跟初次 goto 行为一致(pageIndex=3,见
    // "off-by-one 回归"测试 line 386 的同一断言)
    await waitForAnnotationAfterReload(page, REAL_DOC_ID, 3)
    await reloadN(page, REAL_DOC_ID, 3, RELOAD_N)
  })
})
