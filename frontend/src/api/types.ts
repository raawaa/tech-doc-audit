// 知识库类型
export interface KnowledgeBase {
  id: string
  name: string
  description: string
  category: 'national' | 'industry' | 'enterprise'
  document_count: number
  index_status: 'none' | 'building' | 'ready' | 'failed'
  created_at: string
  updated_at: string
}

export interface KBDocument {
  id: string
  name: string
  original_name: string
  file_type: string
  page_count: number | null
  index_status: string
}

// 待审核文档类型
export interface AuditDocument {
  id: string
  name: string
  original_name: string
  file_type: string
  page_count: number | null
  status: 'uploaded' | 'parsed' | 'indexed' | 'audit_pending' | 'auditing' | 'completed' | 'failed'
  created_at: string
  updated_at: string
  has_structure: boolean
  has_index: boolean
}

// 审核任务类型
export interface AuditTask {
  id: string
  document_id: string
  document_name: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  progress: number
  created_at: string
}

export interface IssueLocation {
  chapter?: string
  clause_number?: string
  page?: number
  original_text: string
}

export interface StandardRef {
  standard_name: string
  standard_id: string
  clause?: string
  requirement?: string
}

export interface AuditIssue {
  id: number
  type: 'compliance' | 'completeness' | 'consistency'
  clause_number?: string
  description: string
  severity: 'high' | 'medium' | 'low'
  standard_name?: string
  standard_clause?: string
  suggestion?: string
}

export interface AuditResult {
  task_id: string
  document_id: string
  document_name: string
  summary: {
    total_clauses: number
    issues_count: number
    compliance_issues: number
    completeness_issues: number
    consistency_issues: number
    high_severity: number
    medium_severity: number
    low_severity: number
  }
  issues: AuditIssue[]
  generated_at: string
}

export interface DocumentStructure {
  doc_id: string
  title?: string
  chapters: {
    number?: string
    title: string
    clauses: { number: string; text: string }[]
  }[]
  total_clauses: number
}

// Q&A 问答类型
export interface QASource {
  kb_id: string
  doc_id: string
  doc_source: string
  content_snippet: string
  relevance: number
}

export interface QAResponse {
  answer: string
  sources: QASource[]
}

export interface QARequest {
  question: string
  kb_ids: string[]
  top_k?: number
}

export interface ChatRequest {
  question: string
  kb_ids: string[]
  session_id?: string
  top_k?: number
}

export interface ChatResponse {
  session_id: string
  answer: string
  sources: QASource[]
}
