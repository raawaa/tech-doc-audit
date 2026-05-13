import { apiClient } from './client'
import type {
  KnowledgeBase,
  KBDocument,
  AuditDocument,
  AuditTask,
  AuditResult,
  DocumentStructure,
} from './types'

// ============ 知识库 API ============

export const kbApi = {
  list: async (category?: string): Promise<KnowledgeBase[]> => {
    const params = category ? { category } : {}
    const resp = await apiClient.get('/knowledge-bases', { params })
    return resp.data
  },

  get: async (id: string): Promise<KnowledgeBase> => {
    const resp = await apiClient.get(`/knowledge-bases/${id}`)
    return resp.data
  },

  create: async (data: { name: string; category: string; description?: string }): Promise<KnowledgeBase> => {
    const resp = await apiClient.post('/knowledge-bases', data)
    return resp.data
  },

  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/knowledge-bases/${id}`)
  },

  reindex: async (id: string): Promise<void> => {
    await apiClient.post(`/knowledge-bases/${id}/reindex`)
  },

  listDocuments: async (kbId: string): Promise<KBDocument[]> => {
    const resp = await apiClient.get(`/knowledge-bases/${kbId}/documents`)
    return resp.data
  },

  importDocument: async (kbId: string, file: File): Promise<KBDocument> => {
    const formData = new FormData()
    formData.append('file', file)
    const resp = await apiClient.post(`/documents/${kbId}/upload`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return resp.data
  },
}

// ============ 待审核文档 API ============

export const auditDocApi = {
  list: async (status?: string): Promise<AuditDocument[]> => {
    const params = status ? { status } : {}
    const resp = await apiClient.get('/audit-documents', { params })
    return resp.data
  },

  get: async (id: string): Promise<AuditDocument> => {
    const resp = await apiClient.get(`/audit-documents/${id}`)
    return resp.data
  },

  upload: async (file: File): Promise<AuditDocument> => {
    const formData = new FormData()
    formData.append('file', file)
    const resp = await apiClient.post('/audit-documents', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return resp.data
  },

  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/audit-documents/${id}`)
  },

  parse: async (id: string): Promise<AuditDocument> => {
    const resp = await apiClient.post(`/audit-documents/${id}/parse`)
    return resp.data
  },

  getStructure: async (id: string): Promise<DocumentStructure> => {
    const resp = await apiClient.get(`/audit-documents/${id}/structure`)
    return resp.data
  },

  process: async (id: string): Promise<AuditDocument> => {
    const resp = await apiClient.post(`/audit-documents/${id}/process`)
    return resp.data
  },
}

// ============ 审核任务 API ============

export const auditTaskApi = {
  list: async (documentId?: string): Promise<AuditTask[]> => {
    const params = documentId ? { document_id: documentId } : {}
    const resp = await apiClient.get('/audit-tasks', { params })
    return resp.data
  },

  get: async (id: string): Promise<AuditTask> => {
    const resp = await apiClient.get(`/audit-tasks/${id}`)
    return resp.data
  },

  create: async (data: {
    document_id: string
    kb_ids: string[]
    audit_types?: string[]
    async_mode?: boolean
  }): Promise<AuditTask> => {
    const resp = await apiClient.post('/audit-tasks', data)
    return resp.data
  },

  run: async (id: string, asyncMode: boolean = true): Promise<void> => {
    await apiClient.post(`/audit-tasks/${id}/run`, null, { params: { async_mode: asyncMode } })
  },

  getResult: async (id: string): Promise<AuditResult> => {
    const resp = await apiClient.get(`/audit-tasks/${id}/result`)
    return resp.data
  },

  cancel: async (id: string): Promise<void> => {
    await apiClient.delete(`/audit-tasks/${id}`)
  },
}
