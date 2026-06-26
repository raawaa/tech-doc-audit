import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Download, FileDown, Loader2, Quote } from 'lucide-react'
import { auditTaskApi } from '../api/endpoints'
import type { AuditResult as AuditResultData, AuditIssue } from '../api/types'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge, SeverityDot } from '../components/Badge'

const typeLabels: Record<string, string> = {
  compliance: '合规性',
  completeness: '完整性',
  consistency: '一致性',
  insufficient_evidence: '证据不足',
  out_of_scope: '超出范围',
}

const severityLabels: Record<string, string> = {
  high: '高', medium: '中', low: '低',
}

// ── Markdown 导出 ──────────────────────────────────────────────────────────────

function buildMarkdown(result: AuditResultData): string {
  const { summary, issues, document_name, generated_at } = result
  const date = new Date(generated_at).toLocaleString('zh-CN')

  const lines: string[] = [
    `# 审核报告`,
    '',
    `**文档名称**: ${document_name}`,
    `**生成时间**: ${date}`,
    `**条款总数**: ${summary.total_clauses}`,
    '',
    '## 摘要',
    '',
    '| 指标 | 数量 |',
    '|------|------|',
    `| 发现问题 | ${summary.issues_count} |`,
    `| 合规性问题 | ${summary.compliance_issues} |`,
    `| 完整性问题 | ${summary.completeness_issues} |`,
    `| 一致性问题 | ${summary.consistency_issues} |`,
    `| 高风险 | ${summary.high_severity} |`,
    `| 中风险 | ${summary.medium_severity} |`,
    `| 低风险 | ${summary.low_severity} |`,
    '',
  ]

  if (issues.length === 0) {
    lines.push('## 问题列表', '', '未发现合规问题。', '')
  } else {
    lines.push('## 问题列表', '')
    issues.forEach((issue, i) => {
      lines.push(`### ${i + 1}. [${severityLabels[issue.severity] || issue.severity}] ${issue.description}`)
      lines.push('')
      lines.push(`- **类型**: ${typeLabels[issue.type] || issue.type}`)
      if (issue.clause_number) lines.push(`- **条款编号**: ${issue.clause_number}`)
      if (issue.standard_name) lines.push(`- **标准依据**: ${issue.standard_name}${issue.standard_clause ? ` ${issue.standard_clause}` : ''}`)
      if (issue.document_position) lines.push(`- **文档位置**: ${issue.document_position}`)
      if (issue.suggestion) lines.push(`- **修改建议**: ${issue.suggestion}`)
      if (issue.cited_excerpt) {
        lines.push('')
        lines.push('> ' + issue.cited_excerpt.split('\n').join('\n> '))
      }
      lines.push('')
    })
  }

  return lines.join('\n')
}

function downloadFile(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ── 组件 ──────────────────────────────────────────────────────────────────────

export function AuditResult() {
  const { id: docId, taskId } = useParams<{ id: string; taskId: string }>()
  const navigate = useNavigate()

  const { data: result, isLoading } = useQuery({
    queryKey: ['audit-result', taskId],
    queryFn: () => auditTaskApi.getResult(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => (query.state.data?.summary ? false : 2000),
  })

  if (isLoading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (!result) return <div className="text-center py-20 text-slate-500">暂无结果</div>

  const { summary, issues } = result

  const exportJson = () =>
    downloadFile(`审核报告_${result.document_name}_${Date.now()}.json`, JSON.stringify(result, null, 2), 'application/json')

  const exportMd = () =>
    downloadFile(`审核报告_${result.document_name}_${Date.now()}.md`, buildMarkdown(result), 'text/markdown')

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <button className="btn-ghost btn-sm -ml-2" onClick={() => navigate(`/audit/${docId}`)}>
          <ArrowLeft className="w-4 h-4" /> 返回
        </button>
        <div className="flex gap-2">
          <button className="btn-secondary btn-sm" onClick={exportMd}>
            <FileDown className="w-3.5 h-3.5" /> 导出报告
          </button>
          <button className="btn-ghost btn-sm" onClick={exportJson}>
            <Download className="w-3.5 h-3.5" /> JSON
          </button>
        </div>
      </div>

      <div>
        <h1 className="text-xl font-bold text-slate-900">审核报告</h1>
        <p className="mt-1 text-sm text-slate-500">{result.document_name}</p>
      </div>

      {/* Severity bar — signature element */}
      <Card>
        <CardBody>
          <div className="flex gap-1 h-3 rounded-full overflow-hidden bg-slate-100">
            <div className="bg-red-500 transition-all" style={{ flex: summary.high_severity || 0.01 }} title={`高风险: ${summary.high_severity}`} />
            <div className="bg-amber-500 transition-all" style={{ flex: summary.medium_severity || 0.01 }} title={`中风险: ${summary.medium_severity}`} />
            <div className="bg-emerald-500 transition-all" style={{ flex: summary.low_severity || 0.01 }} title={`低风险: ${summary.low_severity}`} />
          </div>
          <div className="flex justify-center gap-6 mt-3 text-xs text-slate-500">
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-500" /> 高风险 {summary.high_severity}</span>
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-amber-500" /> 中风险 {summary.medium_severity}</span>
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-emerald-500" /> 低风险 {summary.low_severity}</span>
          </div>
        </CardBody>
      </Card>

      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: '条款总数', value: summary.total_clauses, color: 'text-slate-900' },
          { label: '发现问题', value: summary.issues_count, color: 'text-red-600' },
          { label: '合规性问题', value: summary.compliance_issues, color: 'text-red-600' },
          { label: '完整性问题', value: summary.completeness_issues, color: 'text-amber-600' },
        ].map(({ label, value, color }) => (
          <Card key={label}>
            <CardBody className="text-center py-4">
              <p className={`text-2xl font-bold ${color}`}>{value}</p>
              <p className="text-xs text-slate-500 mt-1">{label}</p>
            </CardBody>
          </Card>
        ))}
      </div>

      {/* Issue list */}
      <Card>
        <CardHeader
          title={`问题列表（${issues.length}）`}
          action={
            <div className="flex flex-wrap gap-2 text-xs">
              {['compliance', 'completeness', 'consistency', 'insufficient_evidence', 'out_of_scope'].map((t) => (
                <span key={t} className="flex items-center gap-1">
                  <Badge value={t} /> <span className="text-slate-400">{typeLabels[t]}</span>
                </span>
              ))}
            </div>
          }
        />
        <CardBody className="p-0">
          {issues.length === 0 ? (
            <div className="text-center py-12 text-sm text-slate-400">未发现合规问题</div>
          ) : (
            <div className="divide-y divide-slate-100">
              {issues.map((issue: AuditIssue) => (
                <div key={issue.id} className="px-5 py-4 hover:bg-slate-50/50 transition-colors">
                  <div className="flex items-start gap-3">
                    <SeverityDot severity={issue.severity} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge value={issue.type} />
                        {issue.clause_number && (
                          <span className="text-xs font-mono text-slate-400">#{issue.clause_number}</span>
                        )}
                      </div>
                      {issue.document_position && (
                        <p className="text-xs text-slate-500 mt-0.5 mb-1">📍 {issue.document_position}</p>
                      )}
                      <p className="text-sm text-slate-900 leading-relaxed">{issue.description}</p>

                      {/* 原文引用 */}
                      {issue.cited_excerpt && (
                        <div className="mt-2 flex gap-1.5 text-xs text-slate-600 bg-slate-50 rounded-md p-2.5 border border-slate-100">
                          <Quote className="w-3 h-3 text-slate-400 shrink-0 mt-0.5" />
                          <p className="leading-relaxed">{issue.cited_excerpt}</p>
                        </div>
                      )}

                      {/* 标准依据 + 建议 */}
                      {(issue.standard_name || issue.suggestion) && (
                        <div className="mt-2 text-xs text-slate-500 bg-blue-50/40 rounded-md p-2.5 border border-blue-100/60">
                          {issue.standard_name && (
                            <p>
                              <span className="font-medium text-slate-600">依据：</span>
                              {issue.standard_doc_id ? (
                                <a
                                  href={`/pdf-viewer/${issue.standard_doc_id}?page=${issue.standard_page_number ?? ''}&clause=${encodeURIComponent(issue.standard_clause || '')}&highlight=${encodeURIComponent(issue.standard_chunk_text || '')}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-blue-600 hover:underline cursor-pointer"
                                >
                                  📄 {issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}
                                </a>
                              ) : (
                                <span>{issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}</span>
                              )}
                            </p>
                          )}
                          {issue.suggestion && <p className="mt-1"><span className="font-medium text-slate-600">建议：</span>{issue.suggestion}</p>}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  )
}
