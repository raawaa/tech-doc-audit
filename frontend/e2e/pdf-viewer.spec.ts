import { test, expect, type Page } from '@playwright/test'

/**
 * PDF viewer 端到端测试 (V8-S6 后 / embedpdf 重写后)。
 *
 * 覆盖三类入口：
 * 1. 直接打开 /pdf-viewer/:docId —— 2026-07-03 "PDF 没显示" bug 的回归
 * 2. 从审核结果页点 standard 链接跳过去 —— 用户实际使用路径
 * 3. URL 契约 ?page= / ?block_range= / ?highlight= 行为
 *
 * 核心断言：PDF 页面在 viewport 内渲染,高亮是 page wrapper 上的
 * `[data-testid="highlight-rect"]` div(不是 canvas fillRect)。
 */

const PDF_DOC_ID = '01KW10F4SD4BZG6SQFNQ42JDTH'
const AUDIT_DOC_ID = '01KVY36VHQGD6S3SBH5JWRF7ZS'
const AUDIT_TASK_ID = '01KWK9SR726Q57SXK9V7NVC9GB'
const REAL_DOC_ID = '01KW1QXZ5AKBGK34BDJRV1X4JZ'

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
      body: '%PDF-1.4 dummy',
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

test.describe('PDF viewer (embedpdf)', () => {
  test('直接打开 /pdf-viewer/:docId 时 [data-testid="pdf-viewer"] 在视口内', async ({ page }) => {
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=&clause=&highlight=GB%2050016`)
    await expect(page.getByTestId('pdf-viewer')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('pdf-page').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('highlight-rect')).toHaveCount(0)
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

  // ── Slice 1: highlight overlay DOM 契约 ────────────────────────────────

  test('合法 doc + block_range 参数：layout 命中 → highlight-rect 数 = 命中 block 数', async ({ page }) => {
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
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    const hits = page.locator('[data-page-index="0"] [data-testid="highlight-rect"]')
    await expect(hits).toHaveCount(3, { timeout: 10_000 })
  })

  test('合法 doc + highlight 字符串参数：layout 命中 → highlight-rect 渲染', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'GB 50016 是一个建筑防火设计规范', y1: 0.05, y2: 0.10, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('GB 50016')}`,
    )
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    const hits = page.locator('[data-testid="highlight-rect"]')
    await expect(hits).toHaveCount(1, { timeout: 10_000 })
  })

  test('未 reparse doc + 带 highlight：E1 fallback UI 出现', async ({ page }) => {
    await mockLayoutNotFound(page)
    await mockMeta(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('800兆对讲机')}`,
    )
    await expect(page.getByText(/该文档未解析/)).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: /重新解析/ })).toBeVisible()
  })

  // ── Slice 1: DOCX/MD fallback 路径(issue #63 acceptance)─────────────

  test('DOCX doc：text-fallback 路径渲染 + header 显示共 N 页', async ({ page }) => {
    await page.route('**/api/v1/kb-documents/*/page/*', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ text: '这是 DOCX 第 1 页正文内容', total_pages: 5 }),
      }),
    )
    await mockMeta(page, { file_type: 'docx', page_count: 5, name: 'report.docx' })
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=2`)
    // text-fallback 容器出现 + 显示共 5 页(来自 /page API)
    await expect(page.getByTestId('text-fallback')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/第 2 \/ 5 页/)).toBeVisible()
  })

  // ── Slice 1: scrollbar 稳定性回归（PRD 的核心 bug）─────────────────────

  test('拖到文档中部停留 5s：scrollTop / scrollHeight 唯一值计数 ≤ 3', async ({ page }) => {
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

  // ── Slice 2: URL 契约 — auto-jump-to-first-hit ─────────────────────────

  test('?page=1&highlight=<非首页条款名>：auto-jump 到首匹配页', async ({ page }) => {
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

  // ── Slice 2: URL 契约 — off-by-one 回归（DOM-based）───────────────────

  test('?page=1&highlight=<非首页条款名>：highlight-rect 落在正确 page-index 元素内', async ({ page }) => {
    await page.goto(
      `/pdf-viewer/${REAL_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`,
    )
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })

    await expect.poll(async () => {
      return await page.locator('[data-testid="highlight-rect"]').count()
    }, { timeout: 30_000, intervals: [500, 1000, 2000] }).toBeGreaterThan(0)

    // highlight-rect 必须在 data-page-index="3"（0-based）容器内,
    // 不能在 data-page-index="2"。Bug A:0-based ↔ 1-based 错位。
    const inPage3 = await page.locator('[data-page-index="3"] [data-testid="highlight-rect"]').count()
    const inPage2 = await page.locator('[data-page-index="2"] [data-testid="highlight-rect"]').count()
    expect(inPage3).toBeGreaterThan(0)
    expect(inPage2).toBe(0)
  })

  // ── Slice 2: block_range 多 block / 单 block ──────────────────────────

  test('block_range=3,3 单 block：1 个 highlight-rect', async ({ page }) => {
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
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('[data-testid="highlight-rect"]')).toHaveCount(1, { timeout: 10_000 })
  })

  test('block_range=2,5 多 block：4 个 highlight-rect（每 block 一个）', async ({ page }) => {
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
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('[data-testid="highlight-rect"]')).toHaveCount(4, { timeout: 10_000 })
  })

  // ── Slice 2: block_range 未命中 → fallback 到 highlight ───────────────

  test('block_range 区间无 blocks 命中 + 同 URL 带 highlight：fallback 到 highlight 全页扫描', async ({ page }) => {
    // layout 里只有 block_order=0,1;block_range=5,5 不会命中任何 block。
    // highlight='fallback text' 在 block_order=0 命中,所以 fallback 路径应画
    // 1 个 highlight-rect。
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
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    // block_range 没命中,但 highlight 命中 → 应有 1 个 highlight-rect
    await expect(page.locator('[data-testid="highlight-rect"]')).toHaveCount(1, { timeout: 10_000 })
  })

  // ── Slice 2: legacy ?highlight= 兼容（无 block_range）────────────────

  test('旧 ?highlight= 无 block_range：仍能渲染 highlight-rect', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: '应急救援指挥中心是核心', y1: 0.05, y2: 0.10, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('应急救援指挥中心')}`,
    )
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('[data-testid="highlight-rect"]')).toHaveCount(1, { timeout: 10_000 })
  })

  // ── Slice 2: header 跳页输入框 + Enter ────────────────────────────────

  test('header page-jump-input + Enter：跳到指定页', async ({ page }) => {
    await mockMeta(page, { name: 'jump-test.pdf', page_count: 10 })
    await mockPdfFile(page)
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=1`)
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })

    // 输入页码 + Enter
    const input = page.getByTestId('page-jump-input')
    await input.fill('5')
    await input.press('Enter')

    // 等 scroll 完成
    await expect.poll(async () => {
      const txt = await page.getByTestId('page-counter').textContent()
      const m = txt?.match(/^(\d+)\s*\/\s*(\d+)/)
      return m ? Number(m[1]) : -1
    }, { timeout: 10_000, intervals: [200, 500] }).toBe(5)
  })
})