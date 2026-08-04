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
// ── 批量重新解析 (Bulk Reparse) ──
//
// 三个端点共用同一形状的源数据（spec #102 / issue #111）：
//   - GET  /bulk-reparse/preflight  → 预检
//   - POST /bulk-reparse            → 触发
//   - GET  /bulk-reparse/report     → 报告
//
// ``reason`` 取值见 ``services/bulk_reparse_service``：
//   入选：not_embedded / missing_pages / empty_layout / forced
//   跳过：page_limit
export interface BulkReparseTarget {
  doc_id: string
  original_name: string
  page_count: number | null
  reason: string
  cache_state: 'cached' | 'uncached'
}

export interface BulkReparseOverPageLimit {
  doc_id: string
  original_name: string
  page_count: number
  reason: string
}

export interface BulkReparsePreflight {
  kb_id: string
  force: boolean
  target_count: number
  cached_docs: number
  uncached_docs: number
  polluted_cached_docs: number
  cached_pages: number
  uncached_pages: number
  estimated_ocr_pages: number
  targets: BulkReparseTarget[]
  over_page_limit: BulkReparseOverPageLimit[]
}

export interface BulkReparseTriggerRequest {
  concurrency?: number
  force?: boolean
}

export interface BulkReparseTriggerResponse {
  kb_id: string
  target_count: number
  index_status: 'building'
}

export interface BulkReparseReportPreflight {
  cached_docs: number
  uncached_docs: number
  estimated_cached_pages: number
  targets: BulkReparseTarget[]
}

export interface BulkReparseReportEntry {
  doc_id: string
  original_name: string
  reason: string
  // done 篇携带解析来源与页数；failed 篇仅 reason 必填
  source?: string
  pages?: number
  page_count?: number
}

export interface BulkReparseReport {
  schema_version: number
  kb_id: string
  started_at: string
  finished_at: string
  duration_seconds: number
  forced: boolean
  concurrency: number
  target_count: number
  // 预检估算 vs 实测并列（spec #102 story 26：差异是信号不是拦截）
  estimated_ocr_pages: number
  actual_ocr_pages: number
  actual_pages_by_source: Record<string, number>
  actual_docs_by_source: Record<string, number>
  preflight: BulkReparseReportPreflight
  counts: {
    done: number
    failed: number
    skipped: number
  }
  done: BulkReparseReportEntry[]
  failed: BulkReparseReportEntry[]
  skipped: BulkReparseReportEntry[]
}

export interface QASource {
  kb_id: string
  doc_id: string
  doc_source: string
  content_snippet: string
  page_number?: number | null
  relevance: number
  // V8-S7: 正向 block_range 坐标；后端 qa_service 在 V8-S3 已透传，前端按优先顺序消费
  block_range?: [number, number] | null
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
