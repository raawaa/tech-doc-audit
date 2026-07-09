import { Routes, Route, Navigate } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { HealthBanner } from './components/HealthBanner'
import { AuditDashboard } from './pages/AuditDashboard'
import { AuditDocDetail } from './pages/AuditDocDetail'
import { AuditResult } from './pages/AuditResult'
import { KnowledgeBases } from './pages/KnowledgeBases'
import { KnowledgeBaseDetail } from './pages/KnowledgeBaseDetail'
import { QA } from './pages/QA'
import { PdfViewer } from './pages/PdfViewer'
import { PdfViewerDropin } from './pages/PdfViewerDropin'

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
            <Route path="/pdf-viewer/:docId" element={<PdfViewer />} />
            <Route path="/pdf-viewer-dropin/:docId" element={<PdfViewerDropin />} />
          </Routes>
        </div>
        </main>
      </div>
    </div>
  )
}
