// ── 知识库 ──
// KB 索引状态字段 per ADR-0003：
//   none / building / searchable / failed
// 终态词已分裂：KB 用 'searchable'（不是 'ready'）；doc 用 'embedded'。
export interface KnowledgeBase {
  id: string
  name: string
  description: string
  category: 'national' | 'industry' | 'enterprise'
  document_count: number
  index_status: 'none' | 'building' | 'searchable' | 'failed'
  index_progress: number
  index_current_doc: string
  created_at: string
  updated_at: string
}

export interface KBDocument {
  id: string
  name: string
  original_name: string
  file_type: string
  page_count: number | null
  embedding_status: 'none' | 'pending_index' | 'indexing' | 'embedded' | 'failed'
}

// ── 待审核文档 ──
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

export interface DocumentClause {
  number: string
  text: string
}

export interface DocumentChapter {
  number?: string
  title: string
  clauses: DocumentClause[]
}

export interface DocumentStructure {
  doc_id: string
  title: string | null
  chapters: DocumentChapter[]
  total_clauses: number
}

// ── 审核任务 ──
export interface AuditTask {
  id: string
  document_id: string
  document_name: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  progress: number
  progress_label?: string
  created_at: string
  started_at?: string
  completed_at?: string
  result?: AuditResult
}

export interface AuditIssue {
  id: number
  type: 'compliance' | 'completeness' | 'consistency' | 'insufficient_evidence' | 'out_of_scope'
  clause_number?: string
  description: string
  severity: 'high' | 'medium' | 'low'
  standard_name?: string
  standard_clause?: string
  suggestion?: string
  cited_excerpt?: string
  document_position?: string
  standard_doc_id?: string
  standard_page_number?: number
  standard_chunk_text?: string
  standard_file_type?: string
  // V8-S6: 正向高亮坐标 (start_block_order, end_block_order)。
  // 非空时 PdfViewer 走坐标主路径;缺失/旧 KB 时 fallback 到 highlight 字符串匹配。
  standard_block_range?: [number, number]
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

// ── 流式审核事件 ──
export interface AuditEventIssue {
  id: number
  type: string
  severity: string
  description: string
  standard_name?: string
  standard_clause?: string
  standard_doc_id?: string
  standard_page_number?: number
  standard_chunk_text?: string
  // V8-S6: 同 AuditIssue.standard_block_range —— 由 flag_issue 落地或
  // standard_linker 回填,前端透传到 PdfViewer 走坐标路径。
  standard_block_range?: [number, number]
}

export type AuditEvent =
  | { type: 'start'; message: string }
  | { type: 'reasoning'; content: string }
  | { type: 'tool_call'; tool: string; args: Record<string, unknown> }
  | { type: 'tool_result'; tool: string; content: string; truncated?: boolean }
  | { type: 'issue_found'; issue: AuditEventIssue }
  | { type: 'progress'; message: string }
  | { type: 'complete'; summary: string; issues_count: number }
  | { type: 'cancelled'; message: string }
  | { type: 'error'; message: string }
export interface QASource {
  kb_id: string
  doc_id: string
  doc_source: string
  content_snippet: string
  page_number?: number | null
  relevance: number
}

export interface QAResponse {
  answer: string
  sources: QASource[]
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
