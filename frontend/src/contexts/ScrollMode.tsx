/**
 * ScrollMode — `/pdf-viewer/:docId` 双层 scroll 修复(#71)的 seam。
 *
 * 根因:`<main className="flex-1 overflow-y-auto">` 在所有路由都滚,而 PdfViewer
 * 根容器 `min-h-screen` 把 PDF 模式推到 ≥100vh → 外层 `<main>` 多余可滚;
 * 同时 embedpdf viewer 自己内嵌 scroll → 双层。修复:`<main>` 根据当前路由的
 * "scroll 模式"在 `overflow-y-auto` (default,文本/DOCX 路径需要外层滚动)与
 * `overflow-hidden` (PDF 模式,只让 embedpdf viewer 内嵌滚)之间切换。
 *
 * contract:
 * - mode 由 App.tsx 读,用于 `<main>` overflow 类
 * - mode 由 PdfViewer.tsx 在拿到 `meta.file_type` 后写:
 *     `pdf` → 'hidden'   (embedpdf viewer 自带 scroll,占满 viewport 高度)
 *     `docx`/`md`/其他 → 'default'  (外层 scroll,行为不变)
 * - meta 加载前保持 'default';组件 unmount 时 reset 回 'default'。
 */
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type ScrollMode = 'default' | 'hidden'

interface ScrollModeCtxValue {
  mode: ScrollMode
  setMode: (m: ScrollMode) => void
}

const ScrollModeCtx = createContext<ScrollModeCtxValue>({
  mode: 'default',
  setMode: () => {
    /* no-op default;Provider 注入真实现 */
  },
})

export function ScrollModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ScrollMode>('default')
  // setMode 引用本身稳定,但 React 要求 Provider value 引用稳定 — 包 useMemo。
  const value = useMemo<ScrollModeCtxValue>(() => ({ mode, setMode }), [mode])
  return (
    <ScrollModeCtx.Provider value={value}>{children}</ScrollModeCtx.Provider>
  )
}

export function useScrollMode(): ScrollModeCtxValue {
  return useContext(ScrollModeCtx)
}
