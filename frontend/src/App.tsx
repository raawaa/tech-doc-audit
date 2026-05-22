import { Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { KnowledgeBasesPage } from './pages/KnowledgeBasesPage'
import { KnowledgeBaseDetailPage } from './pages/KnowledgeBaseDetailPage'
import { AuditDocumentsPage } from './pages/AuditDocumentsPage'
import { AuditDocumentDetailPage } from './pages/AuditDocumentDetailPage'
import { AuditResultPage } from './pages/AuditResultPage'
import { QAPage } from './pages/QAPage'

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/audit" replace />} />
        <Route path="/knowledge-bases" element={<KnowledgeBasesPage />} />
        <Route path="/knowledge-bases/:id" element={<KnowledgeBaseDetailPage />} />
        <Route path="/audit" element={<AuditDocumentsPage />} />
        <Route path="/audit/:id" element={<AuditDocumentDetailPage />} />
        <Route path="/audit/:id/result/:taskId" element={<AuditResultPage />} />
        <Route path="/qa" element={<QAPage />} />
      </Routes>
    </Layout>
  )
}

export default App
