import { Suspense, lazy } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Sidebar } from './components/Sidebar'
import { HealthBanner } from './components/HealthBanner'
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

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden flex-col">
      <HealthBanner />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl px-6 py-8">
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
      </div>
    </div>
  )
}
