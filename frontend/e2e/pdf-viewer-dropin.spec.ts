import { test, expect, type Page } from '@playwright/test'

/**
 * PDF viewer drop-in spike (PRD #66) 端到端测试。
 *
 * 与 ``pdf-viewer.spec.ts``(生产 headless 路径)完全并排;
 * 只测 spike 关心的几条 acceptance 路径,production 行为不在这里覆盖。
 *
 * 测试渠道:
 * - DOM 断言:``data-testid="pdf-viewer-dropin"`` / ``data-testid="page-counter"`` /
 *   ``data-testid="dropin-container"`` 这些都是 spike 独有的标记。
 * - Registry 句柄:spike 在 onReady 时把 PluginRegistry 挂在
 *   ``window.__pdfViewerDropinRegistry`` 上,测用 ``page.evaluate`` 调
 *   ``registry.getPlugin('annotation').provides().forDocument(id).getAnnotations()``
 *   来确认 importAnnotations 真的写进了 annotation 状态。
 *   生产 PdfViewer 没有这条 handle — 它只在 spike 路由上挂(见 PdfViewerDropin.tsx)。
 *
 * Refs:
 * - issue #66 acceptance 1 (viewer 渲染) ✓
 * - issue #66 acceptance 2 (auto-jump + block_range):含 "18" 落在 page-counter
 * - issue #66 acceptance 4 (block_range 2,5 → 4 annotations) ✓
 * - issue #66 acceptance 5 (single-block → 1 annotation) ✓
 * - issue #66 acceptance 6 (color/opacity/position):round-trip 测试断言 strokeColor/opacity/rect
 * - issue #66 acceptance 8 (E1 fallback button) ✓
 * - issue #66 verification risk 2 (PDF 坐标语义):round-trip 测试断言
 *      rect.origin.y == pageH - y2*pageH(证实 Y-flip 方向)
 *
 * 未覆盖(挪到 #68 follow-up):
 * - issue #66 acceptance 7 (refresh 后 PDF 字节未污染)— 需要真实 PDF + 字节对比
 * - issue #66 acceptance 9 (preview build worker:false)— 需要 preview 环境跑
 * - issue #66 acceptance 11 (selection-copy)— 需要交互验证
 */

const DROPIN_DOC_ID = '01KW10F4SD4BZG6SQFNQ42JDTH'

async function waitForDropinRegistry(page: Page, timeoutMs = 30_000) {
  await expect
    .poll(
      async () =>
        (await page.evaluate(
          () => !!(window as unknown as { __pdfViewerDropinRegistry?: unknown })
            .__pdfViewerDropinRegistry,
        )) === true,
      { timeout: timeoutMs, intervals: [500, 1000, 2000] },
    )
    .toBe(true)
}

async function getAnnotationCount(page: Page): Promise<number> {
  // 通过 spike 挂的 registry 句柄拿 annotation 列表长度
  return (await page.evaluate(async (docId) => {
    const reg = (window as unknown as {
      __pdfViewerDropinRegistry?: Promise<{
        getPlugin: (id: string) => {
          provides?: () => {
            forDocument: (d: string) => {
              getAnnotations: (opts?: { pageIndex?: number }) => unknown[]
            }
          } | null
        } | null
      }>
    }).__pdfViewerDropinRegistry
    if (!reg) return -1
    const r = await reg
    const ann = r.getPlugin('annotation')?.provides?.()
    if (!ann) return -2
    return ann.forDocument(docId).getAnnotations().length
  }, DROPIN_DOC_ID)) as number
}

async function getFirstAnnotationPayload(page: Page): Promise<{
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
  return (await page.evaluate(async (docId) => {
    const reg = (window as unknown as {
      __pdfViewerDropinRegistry?: Promise<{
        getPlugin: (id: string) => {
          provides?: () => {
            forDocument: (d: string) => {
              getAnnotations: (opts?: { pageIndex?: number }) => Array<{
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
    }).__pdfViewerDropinRegistry
    if (!reg) return null
    const r = await reg
    const ann = r.getPlugin('annotation')?.provides?.()
    if (!ann) return null
    const all = ann.forDocument(docId).getAnnotations()
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
  }, DROPIN_DOC_ID)) as Awaited<ReturnType<typeof getFirstAnnotationPayload>>
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

async function mockPdfFile(page: Page) {
  await page.route('**/api/v1/kb-documents/*/file', route =>
    route.fulfill({
      status: 200,
      body: '%PDF-1.4 dummy',
      contentType: 'application/pdf',
    }),
  )
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

test.describe('PDF viewer drop-in spike (PRD #66)', () => {
  // ── acceptance #1: 打开 /pdf-viewer-dropin/:docId,viewer shell 在视口内 ──

  test('打开 drop-in 路由:viewer 容器渲染,page-counter 可见', async ({ page }) => {
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(`/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=1`)
    await expect(page.getByTestId('pdf-viewer-dropin')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('dropin-container')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('page-counter')).toBeVisible({ timeout: 30_000 })
  })

  // ── acceptance #4: block_range=2,5 → 4 annotations 一一对应 ────────

  test('block_range=2,5 + mock layout:annotation 数 = 4', async ({ page }) => {
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
      `/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=1&block_range=${encodeURIComponent('2,5')}`,
    )
    await waitForDropinRegistry(page)
    await expect.poll(() => getAnnotationCount(page), {
      timeout: 30_000,
      intervals: [500, 1000, 2000],
    }).toBe(4)
  })

  // ── acceptance #5: block_range=3,3 单 block → 1 annotation ──────────

  test('block_range=3,3 单 block:annotation 数 = 1', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.05, y2: 0.10, order: 0 },
      { content: 'block 1', y1: 0.15, y2: 0.20, order: 1 },
      { content: 'block 2', y1: 0.25, y2: 0.30, order: 2 },
      { content: 'block 3', y1: 0.35, y2: 0.40, order: 3 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=1&block_range=${encodeURIComponent('3,3')}`,
    )
    await waitForDropinRegistry(page)
    await expect.poll(() => getAnnotationCount(page), {
      timeout: 30_000,
      intervals: [500, 1000, 2000],
    }).toBe(1)
  })

  // ── 替换原 #66 复盘的"刷新后 annotation 数 = 0 (commit 未调)"误判测试 ──
  //
  // issue #66 acceptance #7 真正想问的是"importAnnotations 不调 commit,
  // 是否会污染源 PDF 字节"。该断言需要拿到原 PDF 文件 + 重新导出后对比
  // 字节,frontend/e2e 用占位 PDF 无法完成。已挪到 issue #68 follow-up。
  //
  // 当前测试改成 round-trip 路径上的可证伪断言:rect 在 PDF 用户空间、
  // strokeColor / opacity 与构造时一致。

  test('round-trip:rect 是 PDF 用户空间(左下原点,Y-up),strokeColor / opacity / segmentRects 与构造值一致', async ({ page }) => {
    // makeLayout 的 pageW=1000,pageH=2000。bbox_norm [0.05,0.1,0.95,0.2]
    // → 期望 rect: origin (50, 1600), size (900, 200)。
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.1, y2: 0.2, order: 0 },
    ]))
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=1&block_range=${encodeURIComponent('0,0')}`,
    )
    await waitForDropinRegistry(page)

    const payload = await getFirstAnnotationPayload(page)
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
    // 没调 commit 时,commitState 应该是 'new' 而不是 'dirty'/'synced'
    expect(payload!.commitState).toBe('new')
  })

  // ── acceptance #2: 跳到指定 page。drop-in 在 dev + dummy PDF 下 ──────────
  //
  // 加载占位 PDF 时,embedpdf 的 totalPages 永不变 0(spike 文档已知),所以
  // counter 走 fallback 分支 `${targetPage} / ${pageCount}`。
  // 我们只能断言 "18" 出现在 counter 里 — 真正的 auto-jump 验证需要在
  // preview 跑 + 真实 PDF fixture 下做(挪到 #68 follow-up)。

  test('?page=18 + block_range=0,0:page-counter 含 "18"', async ({ page }) => {
    await mockLayoutWith(page, makeLayout([
      { content: 'block 0', y1: 0.1, y2: 0.2, order: 0 },
    ]))
    await mockMeta(page, { page_count: 22 })
    await mockPdfFile(page)
    await page.goto(
      `/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=18&block_range=${encodeURIComponent('0,0')}`,
    )
    await expect(page.getByTestId('page-counter')).toContainText('18', { timeout: 30_000 })
  })

  // ── acceptance #8: E1 重新解析按钮在 wrapper 外层正常显示 ────────────

  test('layout 404 + URL 带 highlight:E1 fallback button 出现', async ({ page }) => {
    await mockLayoutNotFound(page)
    await mockMeta(page)
    await page.goto(
      `/pdf-viewer-dropin/${DROPIN_DOC_ID}?page=1&highlight=${encodeURIComponent('800兆对讲机')}`,
    )
    await expect(page.getByText(/该文档未解析/)).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: /重新解析/ })).toBeVisible()
  })

  // ── production path 不变: /pdf-viewer/:docId 仍然是 headless ──────────

  test('side-by-side 验证:/pdf-viewer/(生产) 仍渲染 headless shell', async ({ page }) => {
    await mockMeta(page)
    await mockPdfFile(page)
    await page.goto(`/pdf-viewer/${DROPIN_DOC_ID}?page=1`)
    await expect(page.getByTestId('pdf-viewer')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('pdf-viewer-dropin')).toHaveCount(0)
  })
})
