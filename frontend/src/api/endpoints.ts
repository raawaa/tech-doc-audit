import { api } from './client'
import type {
  KnowledgeBase, KBDocument,
  AuditDocument, AuditTask, AuditResult,
  QAResponse, ChatRequest, ChatResponse,
  DocumentStructure,
} from './types'

// ── 知识库 ──
export const kbApi = {
  list: (category?: string) =>
    api.get<KnowledgeBase[]>('/knowledge-bases', { params: { category } }).then(r => r.data),
  get: (id: string) =>
    api.get<KnowledgeBase>(`/knowledge-bases/${id}`).then(r => r.data),
  create: (data: { name: string; description?: string; category: string }) =>
    api.post<KnowledgeBase>('/knowledge-bases', data).then(r => r.data),
  delete: (id: string) =>
    api.delete(`/knowledge-bases/${id}`),
  reindex: (id: string) =>
    api.post(`/knowledge-bases/${id}/reindex`),
  documents: {
    list: (kbId: string) =>
      api.get<KBDocument[]>(`/knowledge-bases/${kbId}/documents`).then(r => r.data),
    import: (kbId: string, file: File) => {
      const form = new FormData()
      form.append('file', file)
      return api.post<KBDocument>(`/documents/${kbId}/upload`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      }).then(r => r.data)
    },
    batchImport: (kbId: string, files: File[]) => {
      const form = new FormData()
      files.forEach((f) => form.append('files', f))
      return api.post<{ total: number }>(`/documents/${kbId}/batch-upload`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      }).then(r => r.data)
    },
    delete: (kbId: string, docId: string) =>
      api.delete(`/knowledge-bases/${kbId}/documents/${docId}`),
  },
}

// ── 待审核文档 ──
export const auditDocApi = {
  list: (status?: string) =>
    api.get<AuditDocument[]>('/audit-documents', { params: { status } }).then(r => r.data),
  get: (id: string) =>
    api.get<AuditDocument>(`/audit-documents/${id}`).then(r => r.data),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<AuditDocument>('/audit-documents', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data)
  },
  delete: (id: string) =>
    api.delete(`/audit-documents/${id}`),
  parse: (id: string) =>
    api.post<AuditDocument>(`/audit-documents/${id}/parse`).then(r => r.data),
  process: (id: string) =>
    api.post<AuditDocument>(`/audit-documents/${id}/process`).then(r => r.data),
  getStructure: (id: string) =>
    api.get<DocumentStructure>(`/audit-documents/${id}/structure`).then(r => r.data),
}

// ── 审核任务 ──
export const auditTaskApi = {
  list: (documentId?: string) =>
    api.get<AuditTask[]>('/audit-tasks', { params: { document_id: documentId } }).then(r => r.data),
  get: (id: string) =>
    api.get<AuditTask>(`/audit-tasks/${id}`).then(r => r.data),
  create: (data: { document_id: string; kb_ids: string[]; async_mode?: boolean }) =>
    api.post<AuditTask>('/audit-tasks', data).then(r => r.data),
  run: (id: string, asyncMode = true) =>
    api.post<AuditTask>(`/audit-tasks/${id}/run`, null, { params: { async_mode: asyncMode } }).then(r => r.data),
  getResult: (id: string) =>
    api.get<AuditResult>(`/audit-tasks/${id}/result`).then(r => r.data),
  cancel: (id: string) =>
    api.delete(`/audit-tasks/${id}`),
}

// ── 问答 ──
export const qaApi = {
  ask: (kbIds: string[], question: string, topK = 5) =>
    api.post<QAResponse>('/qa/ask', { kb_ids: kbIds, question, top_k: topK }).then(r => r.data),
  chat: (data: ChatRequest) =>
    api.post<ChatResponse>('/qa/chat', data).then(r => r.data),
}
