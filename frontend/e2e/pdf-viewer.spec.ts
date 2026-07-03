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
})
