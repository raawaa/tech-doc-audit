import { test, expect } from '@playwright/test'

/**
 * PDF viewer 端到端测试。
 *
 * 覆盖两类入口：
 * 1. 直接打开 /pdf-viewer/:docId —— 2026-07-03 "PDF 没显示" bug 的回归
 * 2. 从审核结果页点 standard 链接跳过去 —— 用户实际使用路径
 *
 * 核心断言：PDF canvas 必须实际渲染并落在视口内。
 * 不能只看 DOM 里有 canvas —— 之前的 bug 里 canvas 在 DOM 里存在、
 * toDataURL() 也有内容，但 Document 容器塌缩到 0px 导致视觉不可见。
 * 所以这里用 elementsFromPoint 确认 canvas 在视口里实际可命中的位置。
 */

// 真实环境里的 PDF doc + 完成态 audit task。
// 两者来自线上数据库（2026-07-03 验证）。
const PDF_DOC_ID = '01KW10F4SD4BZG6SQFNQ42JDTH' // 50-GB50034-2013.pdf
const AUDIT_DOC_ID = '01KVY36VHQGD6S3SBH5JWRF7ZS'
const AUDIT_TASK_ID = '01KWK9SR726Q57SXK9V7NVC9GB'

test.describe('PDF viewer', () => {
  test('直接打开 /pdf-viewer/:docId 时 canvas 渲染在视口内', async ({ page }) => {
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=&clause=&highlight=GB%2050016`)

    // 等 react-pdf 完成 Document 加载 + 第一页渲染
    // Document 加载触发 setNumPages，header 会显示 "共 X 页"
    await expect(page.getByText(/页 \/ 共 \d+ 页/)).toBeVisible({ timeout: 15_000 })

    // Document 容器必须有非零高度（之前的 bug 这里 height=0）
    const docHeight = await page.locator('.react-pdf__Document').evaluate(
      el => el.getBoundingClientRect().height,
    )
    expect(docHeight).toBeGreaterThan(100)

    // 第一个 canvas 必须在视口内且被 elementsFromPoint 命中
    // 用 elementsFromPoint 而非 getBoundingClientRect，因为后者在 0 高度布局里
    // 也能返回非零 rect（layout 容错），只有真正可命中的位置才是用户看到的。
    const canvas = page.locator('canvas').first()
    await expect(canvas).toBeVisible()

    const visibleAtCenter = await page.evaluate(() => {
      const c = document.querySelector('canvas')
      if (!c) return false
      const r = c.getBoundingClientRect()
      const cx = r.left + r.width / 2
      const cy = r.top + r.height / 2
      // 视口内且该点上的第一个元素是 canvas（说明 canvas 真的在屏幕上）
      return cx >= 0 && cx <= window.innerWidth
          && cy >= 0 && cy <= window.innerHeight
          && document.elementFromPoint(cx, cy)?.tagName === 'CANVAS'
    })
    expect(visibleAtCenter).toBe(true)
  })

  test('从审核结果页点标准链接能跳转到 PDF viewer 并渲染', async ({ page }) => {
    // 完整链路：审核结果页 → 点 issue 里的标准链接 → 跳到 PDF viewer
    await page.goto(`/audit/${AUDIT_DOC_ID}/result/${AUDIT_TASK_ID}`)

    // 等 issue 列表加载完成（包含 standard_doc_id 的链接会出现）
    const stdLink = page.locator('a[href*="/pdf-viewer/"]').first()
    await expect(stdLink).toBeVisible({ timeout: 15_000 })

    // target="_blank"——在新 tab 打开。监听 popup。
    const [popup] = await Promise.all([
      page.waitForEvent('popup'),
      stdLink.click(),
    ])
    await popup.waitForLoadState('domcontentloaded')

    // popup 必须是 PDF viewer
    expect(popup.url()).toContain(`/pdf-viewer/${PDF_DOC_ID}`)

    // PDF viewer 里第一个 canvas 在视口内
    const canvas = popup.locator('canvas').first()
    await expect(canvas).toBeVisible({ timeout: 15_000 })

    const visibleAtCenter = await popup.evaluate(() => {
      const c = document.querySelector('canvas')
      if (!c) return false
      const r = c.getBoundingClientRect()
      const cx = r.left + r.width / 2
      const cy = r.top + r.height / 2
      return cx >= 0 && cx <= window.innerWidth
          && cy >= 0 && cy <= window.innerHeight
          && document.elementFromPoint(cx, cy)?.tagName === 'CANVAS'
    })
    expect(visibleAtCenter).toBe(true)
  })

  // ── V7.3: layout 高亮 + E1 fallback ──────────────────────────────────────

  test('合法 doc + 带 highlight 参数：layout 命中 → 画布可见黄色高亮（route mock）', async ({ page }) => {
    // 通过 page.route mock 三个 API，断言 ctx.fillRect 被调用且 fillStyle 是黄色。
    await page.route('**/api/v1/kb-documents/*/layout', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          has_layout: true,
          layout: [{
            page: 0,
            width: 1000,
            height: 2000,
            blocks: [{
              block_label: 'text',
              block_content: 'GB 50016 是一个建筑防火设计规范',
              bbox_norm: [0.05, 0.05, 0.95, 0.1],
              polygon_norm: [],
              block_order: 0,
            }],
          }],
        }),
      }),
    )
    await page.route('**/api/v1/kb-documents/*', route => {
      const url = route.request().url()
      if (/\/api\/v1\/kb-documents\/[^/]+$/.test(url)) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 'mock', name: 'mock.pdf', original_name: 'mock.pdf',
            file_type: 'pdf', page_count: 1, kb_id: 'mock_kb',
          }),
        })
      }
      return route.continue()
    })
    // 拦截 PDF 文件请求，避免触发真实下载。
    await page.route('**/api/v1/kb-documents/*/file', route =>
      route.fulfill({
        status: 200, body: '%PDF-1.4 dummy',
        contentType: 'application/pdf',
      }),
    )
    await page.goto(`/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('GB 50016')}`)
    // 等 /layout API 完成
    await page.waitForResponse(r =>
      /\/api\/v1\/kb-documents\/[^/]+\/layout/.test(r.url()),
    )
    // 在 evaluate 上下文中：注入 fillRect 代理，监控 fillStyle 黄色调用
    const highlighted = await page.evaluate(() => new Promise<boolean>(resolve => {
      let found = false
      const observer = new MutationObserver(() => {
        const c = document.querySelector('canvas') as HTMLCanvasElement | null
        if (!c) return
        const ctx = c.getContext('2d')
        if (!ctx) return
        const orig = ctx.fillRect.bind(ctx)
        ctx.fillRect = (x: number, y: number, w: number, h: number) => {
          const fill = String(ctx.fillStyle)
          if (fill.includes('255') && fill.includes('0')) found = true
          return orig(x, y, w, h)
        }
        observer.disconnect()
      })
      observer.observe(document.body, { childList: true, subtree: true })
      // 兜底：2 秒后看是否 highlight 已发生
      setTimeout(() => resolve(found), 2_000)
    }))
    expect(highlighted).toBe(true)
  })

  test('未 reparse doc + 带 highlight：E1 fallback UI 出现（route mock，不依赖真实 db）', async ({ page }) => {
    // 使用 page.route 把 /layout 拦截成本地 404 模拟"未 reparse" 状态。
    // 这样不依赖测试 db 里某个真实未 reparse doc id。
    await page.route('**/api/v1/kb-documents/*/layout', route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '该文档未解析' }),
      }),
    )
    // /meta 也得 mock，否则 metadata fetch 会先 404 看到原始 404 页面，
    // 影响 header 渲染。先把 /meta 设成合法 doc（与 PDF_DOC_ID 同源）
    await page.route('**/api/v1/kb-documents/*', route => {
      const url = route.request().url()
      if (/\/api\/v1\/kb-documents\/[^/]+$/.test(url)) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 'mock', name: 'mock.pdf', original_name: 'mock.pdf',
            file_type: 'pdf', page_count: 1, kb_id: 'mock_kb',
          }),
        })
      }
      return route.continue()
    })
    // 取 docId from URL
    await page.goto(
      `/pdf-viewer/${PDF_DOC_ID}?page=1&highlight=${encodeURIComponent('800兆对讲机')}`,
    )
    // header 应显示 E1 小灰字
    await expect(page.getByText(/该文档未解析/)).toBeVisible({ timeout: 10_000 })
    // \"重新解析\" 按钮存在
    await expect(page.getByRole('button', { name: /重新解析/ })).toBeVisible()
  })
})
