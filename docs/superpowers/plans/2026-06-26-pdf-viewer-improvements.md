# PDF 预览体验改进 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PdfViewer 从单页 canvas 渲染改造为 react-pdf 连续滚动 + 高亮自动定位

**Architecture:** 用 react-pdf 的 `<Document>` + `<Page>` 替代手动 canvas 渲染，保留 pdfjs-dist 用于文本搜索和高亮。一个文件改动，非 PDF 文档逻辑不变。

**Tech Stack:** React 18, TypeScript, react-pdf v10, pdfjs-dist v6, Tailwind CSS

## Global Constraints

- 仅改动 `frontend/src/pages/PdfViewer.tsx`
- 新增依赖 `react-pdf`，不新增其他依赖
- DOCX/MD 降级模式完整保留，行为不变
- 高亮精度保持现有水平（大致位置即可，不要求字符级精确）
- 高亮方式保持 canvas 画黄色半透明矩形

---

### Task 1: 安装 react-pdf 依赖

**Files:**
- Modify: `frontend/package.json`

**Interfaces:**
- Produces: `react-pdf` 可用，版本 ^10.1.0

- [ ] **Step 1: 安装 react-pdf**

```bash
cd frontend && npm install react-pdf
```

- [ ] **Step 2: 验证安装**

```bash
cd frontend && node -e "require('react-pdf'); console.log('react-pdf OK')"
```

Expected: 无报错，输出 `react-pdf OK`

- [ ] **Step 3: 确认 pdfjs-dist 版本兼容**

```bash
cd frontend && node -e "const pkg = require('react-pdf/package.json'); console.log('react-pdf peer pdfjs-dist:', pkg.peerDependencies?.['pdfjs-dist'] || 'not specified')"
```

Expected: 输出版本范围，确认与项目已有的 `pdfjs-dist@^6.0.227` 兼容

- [ ] **Step 4: 复制 pdf.worker 到 public 目录**（如果尚未存在）

react-pdf 需要 pdf.worker，确认 `frontend/public/pdfjs/` 下已有 worker 文件：

```bash
ls -la frontend/public/pdfjs/
```

Expected: 存在 `pdf.worker.min.mjs`（如不存在，从 `node_modules/pdfjs-dist/build/` 复制）

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "chore: add react-pdf dependency"
```

---

### Task 2: 重构 PdfViewer — 导入与 Worker 配置

**Files:**
- Modify: `frontend/src/pages/PdfViewer.tsx`

**Interfaces:**
- Consumes: react-pdf 已安装
- Produces: 文件顶层导入 react-pdf，worker 配置完成

- [ ] **Step 1: 添加 react-pdf 导入，保留 pdfjs-dist 用于文本搜索**

替换文件顶部的导入（第 4-7 行）：

```tsx
import { useEffect, useRef, useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/esm/Page/AnnotationLayer.css'
import 'react-pdf/dist/esm/Page/TextLayer.css'
import * as pdfjsLib from 'pdfjs-dist'
```

旧代码：
```tsx
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import * as pdfjsLib from 'pdfjs-dist'
```

- [ ] **Step 2: 配置 Worker**

替换第 7 行：

```tsx
// 设置 worker — react-pdf 和 pdfjs-dist 共用同一个 worker
const WORKER_SRC = '/pdfjs/pdf.worker.min.mjs'
pdfjs.GlobalWorkerOptions.workerSrc = WORKER_SRC
pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER_SRC
```

旧代码：
```tsx
// 设置 worker
pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdfjs/pdf.worker.min.mjs'
```

- [ ] **Step 3: 验证 TypeScript 编译**

```bash
cd frontend && npx tsc --noEmit src/pages/PdfViewer.tsx 2>&1 | head -20
```

Expected: 无类型错误（如提示缺少 react-pdf 的类型声明，检查 `node_modules/react-pdf` 是否自带类型）

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/PdfViewer.tsx
git commit -m "refactor: add react-pdf imports and worker config"
```

---

### Task 3: 实现连续滚动渲染

**Files:**
- Modify: `frontend/src/pages/PdfViewer.tsx`

**Interfaces:**
- Consumes: Task 2 的导入
- Produces: PDF 文档所有页在滚动容器内连续渲染，react-pdf 自带虚拟滚动

- [ ] **Step 1: 移除旧的状态变量，添加新的**

找到第 26-29 行的状态声明：

```tsx
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [currentPage, setCurrentPage] = useState(targetPage)
  const [totalPages, setTotalPages] = useState(0)
```

替换为：

```tsx
  const [numPages, setNumPages] = useState(0)
  const [pdfDoc, setPdfDoc] = useState<pdfjsLib.PDFDocumentProxy | null>(null)
  const [allPagesRendered, setAllPagesRendered] = useState(false)
  const pageRefs = useRef<Map<number, HTMLCanvasElement>>(new Map())
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const [renderedPages, setRenderedPages] = useState(0)
```

说明：
- `numPages`: PDF 总页数（react-pdf onLoadSuccess 回调提供）
- `pdfDoc`: 保留，用于文本搜索
- `allPagesRendered`: 标记是否所有页已渲染完（用于高亮搜索触发）
- `pageRefs`: 存储每页的 canvas 引用，key 为页码
- `scrollContainerRef`: 滚动容器引用，用于 scrollIntoView
- `renderedPages`: 已渲染页数计数器

- [ ] **Step 2: 用 react-pdf 替换旧的 PDF 加载和渲染 useEffects**

**删除**第 41-55 行的 PDF 加载 useEffect（react-pdf 的 Document 组件内部处理加载）：

```tsx
  // 加载 PDF
  useEffect(() => {
    if (!meta || meta.file_type !== 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const url = `${apiBase}/api/v1/kb-documents/${docId}/file`
    pdfjsLib.getDocument({
      url,
      cMapUrl: '/cmaps/',
      cMapPacked: true,
      wasmUrl: '/pdfjs/wasm/',
    }).promise.then(doc => {
      setPdfDoc(doc)
      setTotalPages(doc.numPages)
    }).catch(e => setError(`PDF 加载失败: ${e.message}`))
  }, [meta, docId])
```

**删除**第 57-92 行的渲染 + 高亮 useEffect（将在 Task 4 中重写高亮逻辑）。

替换为：

```tsx
  // 并行加载 PDF 文档用于文本搜索（浏览器缓存确保只下载一次）
  useEffect(() => {
    if (!meta || meta.file_type !== 'pdf') return
    const apiBase = import.meta.env.VITE_API_BASE_URL || ''
    const url = `${apiBase}/api/v1/kb-documents/${docId}/file`
    pdfjsLib.getDocument({
      url,
      cMapUrl: '/cmaps/',
      cMapPacked: true,
      wasmUrl: '/pdfjs/wasm/',
    }).promise.then(doc => {
      setPdfDoc(doc)
    }).catch(e => setError(`PDF 加载失败: ${e.message}`))
  }, [meta, docId])

  // 追踪 react-pdf 总页数
  function handleDocumentLoadSuccess({ numPages: total }: { numPages: number }) {
    setNumPages(total)
  }

  // 追踪页面渲染完成
  const handlePageRenderSuccess = useCallback((pageNumber: number) => {
    setRenderedPages(prev => {
      const next = prev + 1
      // 当所有页面渲染完成时标记
      if (next >= numPages) {
        // 使用 setTimeout 确保状态更新完成
        setTimeout(() => setAllPagesRendered(true), 100)
      }
      return next
    })
  }, [numPages])

  // 重置渲染计数器（PDF 文档变化时）
  useEffect(() => {
    setRenderedPages(0)
    setAllPagesRendered(false)
    pageRefs.current.clear()
  }, [numPages])
```

- [ ] **Step 3: 替换 JSX 中的内容区域**

替换第 138-153 行的 Content 区域：

旧代码：
```tsx
      {/* Content */}
      <div className="flex justify-center py-6">
        {meta.file_type === 'pdf' ? (
          <div className="bg-white shadow-lg rounded">
            <canvas ref={canvasRef} className="max-w-full" />
          </div>
        ) : (
          <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
            <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {textContent || '（该页无文本内容）'}
            </pre>
            {textTotalPages > 0 && (
              <p className="text-xs text-slate-400 mt-4">第 {targetPage} / {textTotalPages} 页</p>
            )}
          </div>
        )}
      </div>
```

新代码：

```tsx
      {/* Content */}
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-auto"
        style={{ height: 'calc(100vh - 57px)' }}
      >
        {meta.file_type === 'pdf' ? (
          <div className="flex flex-col items-center py-6 gap-4">
            {pdfUrl && (
              <Document
                file={pdfUrl}
                onLoadSuccess={handleDocumentLoadSuccess}
                loading={
                  <div className="flex justify-center py-20">
                    <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
                  </div>
                }
                error={
                  <div className="text-center py-20 text-red-500">
                    PDF 加载失败，请刷新重试
                  </div>
                }
              >
                {Array.from(new Array(numPages), (_, index) => (
                  <Page
                    key={`page_${index + 1}`}
                    pageNumber={index + 1}
                    canvasRef={(ref: HTMLCanvasElement) => {
                      if (ref) {
                        pageRefs.current.set(index + 1, ref)
                      }
                    }}
                    onRenderSuccess={() => handlePageRenderSuccess(index + 1)}
                    renderTextLayer={false}
                    className="bg-white shadow-lg rounded"
                  />
                ))}
              </Document>
            )}
          </div>
        ) : (
          <div className="flex justify-center py-6">
            <div className="bg-white shadow-lg rounded p-8 max-w-3xl w-full">
              <pre className="text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
                {textContent || '（该页无文本内容）'}
              </pre>
              {textTotalPages > 0 && (
                <p className="text-xs text-slate-400 mt-4">第 {targetPage} / {textTotalPages} 页</p>
              )}
            </div>
          </div>
        )}
      </div>
```

注意：需要计算 `pdfUrl`，在组件顶部添加：

```tsx
  const apiBase = import.meta.env.VITE_API_BASE_URL || ''
  const pdfUrl = meta?.file_type === 'pdf' ? `${apiBase}/api/v1/kb-documents/${docId}/file` : null
  const cmapUrl = '/cmaps/'
```

- [ ] **Step 4: 验证 TypeScript 编译**

```bash
cd frontend && npx tsc --noEmit 2>&1 | head -30
```

Expected: 无类型错误。常见问题：
- `canvasRef` 回调类型 → 用 `(ref: HTMLCanvasElement) => void` 即可
- `onLoadSuccess` 参数类型 → `{ numPages: number }`

- [ ] **Step 5: 启动开发服务器目测验证**

```bash
cd frontend && npm run dev
```

打开浏览器，打开一个 PDF 文档页面，确认：
- 所有页面连续渲染
- 可以滚动浏览
- 没有控制台错误

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/PdfViewer.tsx
git commit -m "feat: replace single-page canvas with react-pdf continuous scrolling"
```

---

### Task 4: 实现高亮搜索与自动滚动定位

**Files:**
- Modify: `frontend/src/pages/PdfViewer.tsx`

**Interfaces:**
- Consumes: Task 3 的 `pdfDoc`、`pageRefs`、`allPagesRendered`、`scrollContainerRef`
- Produces: 高亮关键词在所有匹配页上显示黄色矩形，自动滚动到第一个匹配页

- [ ] **Step 1: 添加高亮搜索 + 绘制 useEffect**

在 Task 3 新增的 useEffects 之后，DOCX 降级相关的 useEffect 之前，添加：

```tsx
  // 高亮搜索与自动定位
  useEffect(() => {
    if (!pdfDoc || !highlight || !allPagesRendered) return

    const searchTerms = highlight.split(/\s+/).filter(t => t.length > 1)
    if (searchTerms.length === 0) return

    let firstHighlightedPage: number | null = null

    // 逐页搜索文本
    async function searchAndHighlight() {
      for (let pageNum = 1; pageNum <= numPages; pageNum++) {
        try {
          const page = await pdfDoc.getPage(pageNum)
          const textContent = await page.getTextContent()
          const viewport = page.getViewport({ scale: 1.5 })

          let pageHasMatch = false
          const canvas = pageRefs.current.get(pageNum)
          if (!canvas) continue

          const ctx = canvas.getContext('2d')
          if (!ctx) continue

          for (const item of textContent.items) {
            const textItem = item as { str: string; transform: number[] }
            const str = textItem.str || ''
            for (const term of searchTerms) {
              if (str.includes(term)) {
                pageHasMatch = true
                const tx = textItem.transform
                const scale = 1.5
                const x = tx[4] * scale
                const y = canvas.height - tx[5] * scale
                const w = (str.length * (tx[0] || 8)) * scale * 0.6
                const h = 14
                ctx.fillStyle = 'rgba(255, 255, 0, 0.4)'
                ctx.fillRect(x - 1, y - h, w + 2, h + 4)
              }
            }
          }

          if (pageHasMatch && firstHighlightedPage === null) {
            firstHighlightedPage = pageNum
          }
        } catch {
          // 跳过渲染失败的页面
        }
      }

      // 自动滚动到第一个有高亮的页面
      if (firstHighlightedPage !== null && scrollContainerRef.current) {
        const pageEl = scrollContainerRef.current.querySelector(
          `[data-page-number="${firstHighlightedPage}"]`
        )
        if (pageEl) {
          pageEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }
      }
    }

    searchAndHighlight()
  }, [pdfDoc, highlight, numPages, allPagesRendered])
```

- [ ] **Step 2: 在 Page 组件上添加 data-page-number 属性**

修改 Task 3 中的 `<Page>` 组件，添加一个包装 div 以便 DOM 查询定位：

```tsx
<div key={`page_${index + 1}`} data-page-number={index + 1}>
  <Page
    pageNumber={index + 1}
    canvasRef={(ref: HTMLCanvasElement) => {
      if (ref) {
        pageRefs.current.set(index + 1, ref)
      }
    }}
    onRenderSuccess={() => handlePageRenderSuccess(index + 1)}
    renderTextLayer={false}
    className="bg-white shadow-lg rounded"
  />
</div>
```

- [ ] **Step 3: 验证 TypeScript 编译**

```bash
cd frontend && npx tsc --noEmit 2>&1 | head -30
```

Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/PdfViewer.tsx
git commit -m "feat: add cross-page highlight search and auto-scroll"
```

---

### Task 5: 清理翻页 UI — PDF 模式移除翻页按钮，保留跳转输入框

**Files:**
- Modify: `frontend/src/pages/PdfViewer.tsx`

**Interfaces:**
- Consumes: Task 3-4 的实现
- Produces: Header 中 PDF 翻页按钮被页码跳转输入框替代；非 PDF 模式不变

- [ ] **Step 1: 添加页码跳转逻辑**

在组件顶部添加跳转函数：

```tsx
  const handleJumpToPage = (page: number) => {
    if (!scrollContainerRef.current) return
    const clamped = Math.min(Math.max(page, 1), numPages)
    const pageEl = scrollContainerRef.current.querySelector(
      `[data-page-number="${clamped}"]`
    )
    if (pageEl) {
      pageEl.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }
```

- [ ] **Step 2: 替换 Header 中的翻页控件**

找到 Header 中的翻页控件（第 121-131 行）：

旧代码：
```tsx
          {pdfDoc && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage <= 1} onClick={() => setCurrentPage(p => p - 1)}>←</button>
              <span className="text-slate-600 tabular-nums">{currentPage} / {totalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={currentPage >= totalPages} onClick={() => setCurrentPage(p => p + 1)}>→</button>
              <input type="number" className="w-14 px-2 py-1 border rounded text-center text-xs"
                min={1} max={totalPages} value={currentPage}
                onChange={e => { const v = parseInt(e.target.value); if (v >= 1 && v <= totalPages) setCurrentPage(v) }} />
            </>
          )}
```

替换为：

```tsx
          {/* PDF 模式：页码跳转 */}
          {numPages > 0 && (
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <span>跳至</span>
              <input
                type="number"
                className="w-14 px-2 py-1 border rounded text-center text-xs"
                min={1}
                max={numPages}
                defaultValue={targetPage}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    handleJumpToPage(parseInt((e.target as HTMLInputElement).value))
                  }
                }}
              />
              <span>页 / 共 {numPages} 页</span>
            </div>
          )}
          {/* 非 PDF 模式：保留翻页按钮 */}
          {!numPages && textTotalPages > 0 && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage <= 1} onClick={() => { /* 非 PDF 翻页逻辑保留 */ }}>←</button>
              <span className="text-slate-600 tabular-nums text-sm">{targetPage} / {textTotalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage >= textTotalPages} onClick={() => { /* 非 PDF 翻页逻辑保留 */ }}>→</button>
            </>
          )}
```

注意：非 PDF 翻页逻辑需要通过 searchParams 更新 page 参数，稍作调整为：

```tsx
          {/* 非 PDF 模式：翻页按钮 */}
          {!numPages && textTotalPages > 0 && (
            <>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage <= 1}
                onClick={() => setSearchParams({ page: String(targetPage - 1) })}>←</button>
              <span className="text-slate-600 tabular-nums text-sm">{targetPage} / {textTotalPages}</span>
              <button className="px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-30"
                disabled={targetPage >= textTotalPages}
                onClick={() => setSearchParams({ page: String(targetPage + 1) })}>→</button>
            </>
          )}
```

需要从 `useSearchParams` 解构出 `setSearchParams`：

```tsx
  const [searchParams, setSearchParams] = useSearchParams()
```

（第 19 行，将 `const [searchParams] = useSearchParams()` 改为 `const [searchParams, setSearchParams] = useSearchParams()`）

同时需要删除不再使用的 `currentPage` / `totalPages` 状态变量引用（如果在 Task 3 未清理干净）。

- [ ] **Step 3: 处理边缘情况**

在 `setError` 被调用时（`pdfUrl` 为 null 或异常），确保滚动容器不崩溃。检查 `pdfUrl` 变量是否在组件顶部正确计算：

```tsx
  const apiBase = import.meta.env.VITE_API_BASE_URL || ''
  const pdfUrl = meta?.file_type === 'pdf' ? `${apiBase}/api/v1/kb-documents/${docId}/file` : null
```

- [ ] **Step 4: 验证 TypeScript 编译**

```bash
cd frontend && npx tsc --noEmit 2>&1 | head -30
```

Expected: 无类型错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/PdfViewer.tsx
git commit -m "refactor: replace PDF nav buttons with jump-to-page input"
```

---

### Task 6: 端到端验证与收尾

**Files:**
- Modify: `frontend/src/pages/PdfViewer.tsx`（可能的修复）

**Interfaces:**
- Consumes: Task 1-5 全部完成

- [ ] **Step 1: 完整编译检查**

```bash
cd frontend && npx tsc --noEmit
```

Expected: 零错误

- [ ] **Step 2: 生产构建验证**

```bash
cd frontend && npm run build 2>&1 | tail -20
```

Expected: 构建成功，无错误

- [ ] **Step 3: 启动开发服务器，手动测试以下场景**

```bash
cd frontend && npm run dev
```

测试清单：
- [ ] 打开 PDF 文档 → 所有页连续渲染，可滚动
- [ ] 带 `?highlight=关键词` 参数 → 黄色高亮显示 + 自动滚动到高亮位置
- [ ] 带 `?page=5` 参数 → 初始在顶部，可输入页码跳转
- [ ] 在跳转输入框输入页码按回车 → 滚动到对应页
- [ ] 打开 DOCX 文档 → 降级模式正常，翻页按钮可用
- [ ] 打开不存在的文档 → 显示错误信息
- [ ] 切换不同 PDF → 页面重载正常

- [ ] **Step 4: 如有问题，修复后重新验证**

- [ ] **Step 5: 最终 Commit**

```bash
git add frontend/src/pages/PdfViewer.tsx
git commit -m "chore: final polish for PDF viewer improvements"
```

---

## 文件结构总结

| 文件 | 操作 | 职责 |
|------|------|------|
| `frontend/package.json` | 修改（新增依赖） | react-pdf 依赖声明 |
| `frontend/src/pages/PdfViewer.tsx` | 重构（~150 → ~220 行） | PDF 连续滚动渲染 + 高亮定位 |
| `frontend/public/pdfjs/` | 确认存在 | pdf.worker 文件 |

## 降级与风险

| 风险 | 缓解 |
|------|------|
| react-pdf v10 与 pdfjs-dist v6 不兼容 | Task 1 Step 3 检查 peerDependencies |
| 大 PDF 首屏加载慢 | react-pdf 自带虚拟滚动 + loading 指示器 |
| 双加载 PDF（react-pdf + pdfjs-dist） | 浏览器 HTTP 缓存确保 PDF 字节只下载一次 |
| 高亮在页面未渲染时执行 | `allPagesRendered` 门控 + `canvasRef` 缓存 |
