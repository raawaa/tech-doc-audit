import { Suspense, lazy } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Sidebar } from './components/Sidebar'
import { HealthBanner } from './components/HealthBanner'
import { ScrollModeProvider, useScrollMode } from './contexts/ScrollMode'
import { AuditDashboard } from './pages/AuditDashboard'
import { AuditDocDetail } from './pages/AuditDocDetail'
import { AuditResult } from './pages/AuditResult'
import { KnowledgeBases } from './pages/KnowledgeBases'
import { KnowledgeBaseDetail } from './pages/KnowledgeBaseDetail'
import { QA } from './pages/QA'

// PdfViewer 懒加载:embedpdf drop-in(@embedpdf/react-pdf-viewer + pdfium)
// 体积大(~272 kB gzip),拆成独立 chunk 避免拖累主 index bundle(V9 PRD #68)。
const PdfViewer = lazy(() =>
  import('./pages/PdfViewer').then(m => ({ default: m.PdfViewer })),
)

/**
 * 含 scroll 模式控制的 <main>。mode 由 PdfViewer 在拿到 meta.file_type 后
 * 推入:'pdf' → 'hidden' (外层停滚,让 embedpdf viewer 内嵌滚);
 * 其他 file_type 或未拿到 meta → 'default' (外层 overflow-y-auto 行为不变)。
 */
function MainContent() {
  const { mode } = useScrollMode()
  return (
    <main
      className={
        mode === 'hidden'
          ? 'flex-1 overflow-hidden'
          : 'flex-1 overflow-y-auto'
      }
    >
      <div className="mx-auto max-w-6xl px-6 py-8 h-full flex flex-col">
        <Routes>
          <Route path="/" element={<Navigate to="/audit" replace />} />
          <Route path="/audit" element={<AuditDashboard />} />
          <Route path="/audit/:id" element={<AuditDocDetail />} />
          <Route path="/audit/:id/result/:taskId" element={<AuditResult />} />
          <Route path="/knowledge-bases" element={<KnowledgeBases />} />
          <Route path="/knowledge-bases/:id" element={<KnowledgeBaseDetail />} />
          <Route path="/qa" element={<QA />} />
          <Route
            path="/pdf-viewer/:docId"
            element={
              <Suspense
                fallback={
                  <div className="flex justify-center py-20">
                    <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
                  </div>
                }
              >
                <PdfViewer />
              </Suspense>
            }
          />
        </Routes>
      </div>
    </main>
  )
}

export default function App() {
  return (
    <ScrollModeProvider>
      <div className="flex h-screen overflow-hidden flex-col">
        <HealthBanner />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <MainContent />
        </div>
      </div>
    </ScrollModeProvider>
  )
}
