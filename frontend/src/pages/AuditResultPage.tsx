import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { auditTaskApi } from '@/api'
import { StatusBadge, LoadingSpinner } from '@/components/StatusBadge'
import type { AuditResult } from '@/api/types'

export function AuditResultPage() {
  const { taskId } = useParams<{ taskId: string }>()

  const { data: task, isLoading: taskLoading } = useQuery({
    queryKey: ['audit-task', taskId],
    queryFn: () => auditTaskApi.get(taskId!),
    enabled: !!taskId,
  })

  const { data: result, isLoading: resultLoading } = useQuery({
    queryKey: ['audit-result', taskId],
    queryFn: () => auditTaskApi.getResult(taskId!),
    enabled: !!taskId && task?.status === 'completed',
    retry: false,
  })

  if (taskLoading || resultLoading) return <LoadingSpinner />

  if (task?.status !== 'completed') {
    return (
      <div className="text-center py-12">
        <h2 className="text-xl font-semibold text-gray-900">审核未完成</h2>
        <p className="mt-2 text-gray-500">当前状态: {task?.status}</p>
        <Link to="/" className="mt-4 text-blue-600 hover:text-blue-800">
          返回首页
        </Link>
      </div>
    )
  }

  if (!result) return <div>结果不存在</div>

  return (
    <div>
      <div className="mb-6">
        <Link to={`/audit/${result.document_id}`} className="text-blue-600 hover:text-blue-800 text-sm">
          ← 返回文档详情
        </Link>
      </div>

      {/* Header */}
      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <div className="flex justify-between items-start mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{result.document_name}</h1>
            <p className="mt-1 text-gray-500">审核报告</p>
          </div>
          <button
            onClick={() => exportJson(result)}
            className="px-4 py-2 border border-gray-300 rounded-md text-sm hover:bg-gray-50"
          >
            导出 JSON
          </button>
        </div>
        <p className="text-sm text-gray-500">
          生成时间: {new Date(result.generated_at).toLocaleString()}
        </p>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <SummaryCard title="总条款数" value={result.summary.total_clauses} color="gray" />
        <SummaryCard title="发现问题" value={result.summary.issues_count} color="red" />
        <SummaryCard title="高严重" value={result.summary.high_severity} color="orange" />
        <SummaryCard title="中严重" value={result.summary.medium_severity} color="yellow" />
      </div>

      {/* Issue Categories */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-white shadow rounded-lg p-4">
          <div className="text-sm text-gray-500">合规性问题</div>
          <div className="text-2xl font-bold text-blue-600">{result.summary.compliance_issues}</div>
        </div>
        <div className="bg-white shadow rounded-lg p-4">
          <div className="text-sm text-gray-500">完整性问题</div>
          <div className="text-2xl font-bold text-purple-600">{result.summary.completeness_issues}</div>
        </div>
        <div className="bg-white shadow rounded-lg p-4">
          <div className="text-sm text-gray-500">一致性问题</div>
          <div className="text-2xl font-bold text-green-600">{result.summary.consistency_issues}</div>
        </div>
      </div>

      {/* Issues List */}
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">问题详情 ({result.issues.length})</h2>
        </div>
        {result.issues.length === 0 ? (
          <div className="p-12 text-center">
            <div className="text-6xl mb-4">✓</div>
            <div className="text-xl font-semibold text-green-600">审核通过</div>
            <div className="mt-2 text-gray-500">未发现问题，文档符合标准要求</div>
          </div>
        ) : (
          <ul className="divide-y divide-gray-200">
            {result.issues.map((issue) => (
              <IssueCard key={issue.id} issue={issue} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function SummaryCard({ title, value, color }: { title: string; value: number; color: string }) {
  const colorClasses: Record<string, string> = {
    gray: 'text-gray-900',
    red: 'text-red-600',
    orange: 'text-orange-600',
    yellow: 'text-yellow-600',
    green: 'text-green-600',
    blue: 'text-blue-600',
  }

  return (
    <div className="bg-white shadow rounded-lg p-4 text-center">
      <div className={`text-3xl font-bold ${colorClasses[color]}`}>{value}</div>
      <div className="text-sm text-gray-500 mt-1">{title}</div>
    </div>
  )
}

function IssueCard({ issue }: { issue: AuditResult['issues'][0] }) {
  return (
    <li className="px-6 py-4">
      <div className="flex items-start space-x-3">
        <div className="flex-shrink-0 mt-1">
          {issue.severity === 'high' && <span className="text-red-500 text-xl">●</span>}
          {issue.severity === 'medium' && <span className="text-yellow-500 text-xl">●</span>}
          {issue.severity === 'low' && <span className="text-green-500 text-xl">●</span>}
        </div>
        <div className="flex-1">
          <div className="flex items-center space-x-2 mb-2">
            <StatusBadge status={issue.severity} />
            <span className="text-sm font-medium">
              {issue.type === 'compliance' ? '合规性' : issue.type === 'completeness' ? '完整性' : '一致性'}问题
            </span>
            {issue.clause_number && (
              <span className="text-sm text-gray-500">条款 {issue.clause_number}</span>
            )}
          </div>
          <p className="text-gray-700 mb-2">{issue.description}</p>
          {issue.standard_name && (
            <div className="text-sm text-gray-500">
              <span className="font-medium">依据标准:</span> {issue.standard_name}
              {issue.standard_clause && ` (条款 ${issue.standard_clause})`}
            </div>
          )}
          {issue.suggestion && (
            <div className="mt-2 p-2 bg-blue-50 rounded text-sm">
              <span className="font-medium text-blue-700">修改建议:</span> {issue.suggestion}
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

function exportJson(result: AuditResult) {
  const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `audit-report-${result.task_id}.json`
  a.click()
  URL.revokeObjectURL(url)
}
