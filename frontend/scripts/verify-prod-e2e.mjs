/**
 * E2E 验证脚本: vite preview (production build) + headless chromium
 * 走完三个核心用户场景:
 *   1. 直接打开 /pdf-viewer/:docId -> 渲染、page-counter、pdf-page 可见
 *   2. ?block_range=X,Y -> highlight-rect 出现
 *   3. ?highlight=... -> auto-jump 到首匹配页
 *   4. header page-jump-input Enter -> 跳页 (URL 保留 block_range)
 *
 * 用法:
 *   - 启动 vite preview (npm run build && npx vite preview --port 4173)
 *   - node scripts/verify-prod-e2e.mjs
 *
 * 用途: #65 worker 验证 / #63/#64 端到端冒烟
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:4173'
const PDF_DOC_ID = 'mock-pdf-id'

const browser = await chromium.launch({
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
})
const ctx = await browser.newContext()
const page = await ctx.newPage()

// 拦截后端,提供 mock 数据
await page.route('**/api/v1/kb-documents/*/file', route =>
  route.fulfill({
    status: 200,
    contentType: 'application/pdf',
    body: Buffer.from(
      '%PDF-1.4\n' +
      '1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n' +
      '2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n' +
      '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n' +
      'xref\n0 4\n0000000000 65535 f \n0000000010 00000 n \n0000000053 00000 n \n0000000100 00000 n \n' +
      'trailer<</Size 4/Root 1 0 R>>\nstartxref\n149\n%%EOF',
      'latin1',
    ),
  }),
)
await page.route('**/api/v1/kb-documents/*/layout', route =>
  route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      has_layout: true,
      layout: [{
        page: 0, width: 595, height: 842,
        blocks: [
          { block_label: 'text', block_content: 'hello world', bbox_norm: [0.05, 0.05, 0.95, 0.10], polygon_norm: [], block_order: 0 },
        ],
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

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`  ✓ ${name}`)
  } catch (e) {
    failures++
    console.log(`  ✗ ${name}`)
    console.log(`    ${e.message}`)
  }
}

console.log('=== PRODUCTION E2E 验证 (worker: false) ===')

// 场景 1: 基础渲染
await check('场景 1: /pdf-viewer/:docId 直接打开', async () => {
  await page.goto(`${BASE}/pdf-viewer/${PDF_DOC_ID}?page=1`)
  await page.waitForSelector('[data-testid="pdf-viewer"]', { timeout: 30_000 })
  await page.waitForSelector('[data-testid="page-counter"]', { timeout: 30_000 })
  await page.waitForSelector('[data-testid="pdf-page"]', { timeout: 30_000 })
  const counter = await page.getByTestId('page-counter').textContent()
  if (!counter?.match(/^\d+\s*\/\s*\d+/)) throw new Error(`page-counter 格式异常: ${counter}`)
})

// 场景 2: block_range 命中
await check('场景 2: ?block_range=0,0 渲染 highlight-rect', async () => {
  await page.goto(`${BASE}/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=0,0`)
  await page.waitForSelector('[data-testid="page-counter"]', { timeout: 30_000 })
  await page.waitForSelector('[data-testid="pdf-page"]', { timeout: 30_000 })
  // 等 highlight-rect 出现
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="highlight-rect"]').length > 0,
    { timeout: 10_000 },
  )
  const count = await page.locator('[data-testid="highlight-rect"]').count()
  if (count !== 1) throw new Error(`期望 1 个 highlight-rect,实际 ${count}`)
})

// 场景 3: header 跳页 + URL 保留 block_range
await check('场景 3: header page-jump-input Enter 跳页 + URL 保留 block_range', async () => {
  await page.goto(`${BASE}/pdf-viewer/${PDF_DOC_ID}?page=1&block_range=0,0&highlight=hello`)
  await page.waitForSelector('[data-testid="page-jump-input"]', { timeout: 30_000 })
  const input = page.getByTestId('page-jump-input')
  await input.fill('1')  // mock doc 只有 1 页,所以跳到 1
  await input.press('Enter')
  // URL 必须保留 block_range + highlight (encodeURIComponent 会把逗号编成 %2C,
  // 但 decode 后仍是 "block_range=0,0&highlight=hello")
  const decoded = decodeURIComponent(page.url())
  if (!decoded.includes('block_range=0,0')) throw new Error(`block_range 被 wipe: ${decoded}`)
  if (!decoded.includes('highlight=hello')) throw new Error(`highlight 被 wipe: ${decoded}`)
})

// 场景 4: page-counter 反应 currentPage (embedpdf source of truth)
await check('场景 4: page-counter 反映 embedpdf currentPage', async () => {
  await page.goto(`${BASE}/pdf-viewer/${PDF_DOC_ID}?page=1`)
  await page.waitForSelector('[data-testid="page-counter"]', { timeout: 30_000 })
  // 等 embedpdf onPageChange 触发
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="page-counter"]')
    return el && /^\d+\s*\/\s*\d+\s*页/.test(el.textContent || '')
  }, { timeout: 10_000 })
  const txt = await page.getByTestId('page-counter').textContent()
  if (!txt?.match(/^1\s*\/\s*1\s*页/)) throw new Error(`page-counter 期望 "1 / 1 页",实际 "${txt}"`)
})

await browser.close()

console.log('')
if (failures === 0) {
  console.log('✓ 全部场景通过')
  process.exit(0)
} else {
  console.log(`✗ ${failures} 个场景失败`)
  process.exit(1)
}
