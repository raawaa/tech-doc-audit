import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { readdirSync } from 'node:fs'

// @embedpdf/snippet 把它依赖的 pdfium-worker-engine / pdfium-direct-engine
// 当作 vendored chunk 以相对路径打包进自己的 dist(例如
// `node_modules/@embedpdf/snippet/dist/embedpdf-7TNsu-EA.js` 里就硬编码了
// `await import("./worker-engine-BkD2-rJn.js")` 和 `await import("./direct-engine-...")`)。
// 因此 Rollup 给两条 dynamic-import 各 emit 一个 chunk,即使我们 `PdfViewer.tsx`
// 顶层 `config.worker = false`(沿用 #62/#65 决策)从来不走 worker-engine 分支。
// worker-engine chunk 是死重(gzip ≈119 KB)。
//
// 这个 `enforce: 'pre'` 的 resolveId 把 `./worker-engine-*.js`(无论 importer
// 是 `node_modules/@embedpdf/snippet/dist/embedpdf-7TNsu-EA.js` 还是 dev 模式
// 下的 `node_modules/.vite/deps/chunk-*.js`)重写到同目录下的 `direct-engine-*.js`,
// Rollup 看到两个 specifier 解析到同一 id → dedup → 只 emit 一个 chunk。
// 工程里没有其它相对 `./worker-engine-*.js` 的消费者,放心。
// (issue #69, acceptance 回到 ≤+200 kB gzip)
const collapseWorkerEnginePlugin = {
  name: 'embedpdf-collapse-worker-engine',
  enforce: 'pre' as const,
  resolveId(source: string, importer?: string) {
    if (
      source.startsWith('./worker-engine-') &&
      source.endsWith('.js') &&
      importer
    ) {
      const dir = path.dirname(importer)
      const sibling = readdirSync(dir).find(
        (f) => f.startsWith('direct-engine-') && f.endsWith('.js'),
      )
      if (sibling) return path.join(dir, sibling)
    }
    return null
  },
}

export default defineConfig({
  plugins: [collapseWorkerEnginePlugin, react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
