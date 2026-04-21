import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { auditDocApi, auditTaskApi } from '@/api'
import { StatusBadge, ProgressBar, LoadingSpinner } from '@/components/StatusBadge'

export function AuditDocumentDetailPage() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()

  const { data: doc, isLoading: docLoading } = useQuery({
    queryKey: ['audit-document', id],
    queryFn: () => auditDocApi.get(id!),
    enabled: !!id,
  })

  const { data: tasks, isLoading: tasksLoading } = useQuery({
    queryKey: ['audit-tasks', id],
    queryFn: () => auditTaskApi.list(id),
    enabled: !!id,
  })

  const runTaskMutation = useMutation({
    mutationFn: (taskId: string) => auditTaskApi.run(taskId, false),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-tasks', id] })
    },
  })

  if (docLoading || tasksLoading) return <LoadingSpinner />
  if (!doc) return <div>文档不存在</div>

  const taskList = tasks || []
  const completedTask = taskList.find((t: { status: string }) => t.status === 'completed')

  return (
    <div>
      <div className="mb-6">
        <Link to="/audit" className="text-blue-600 hover:text-blue-800 text-sm">
          ← 返回文档列表
        </Link>
      </div>

      {/* Document Info */}
      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <div className="flex justify-between items-start mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{doc.name}</h1>
            <p className="mt-1 text-gray-500">
              {doc.file_type.toUpperCase()} | {doc.page_count ? `${doc.page_count} 页` : '页数未知'}
            </p>
          </div>
          <StatusBadge status={doc.status} />
        </div>
        <div className="text-sm text-gray-500">
          上传时间: {new Date(doc.created_at).toLocaleString()}
        </div>
      </div>

      {/* Tasks List */}
      <div className="bg-white shadow rounded-lg mb-6">
        <div className="px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">审核任务 ({taskList.length})</h2>
        </div>
        {taskList.length === 0 ? (
          <div className="p-6 text-center text-gray-500">暂无审核任务</div>
        ) : (
          <ul className="divide-y divide-gray-200">
            {taskList.map((task: { id: string; status: string; progress: number; created_at: string }) => (
              <li key={task.id} className="px-6 py-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center space-x-2">
                    <StatusBadge status={task.status} />
                    <span className="text-sm text-gray-500">
                      {new Date(task.created_at).toLocaleString()}
                    </span>
                  </div>
                  {task.status === 'pending' && (
                    <button
                      onClick={() => runTaskMutation.mutate(task.id)}
                      disabled={runTaskMutation.isPending}
                      className="px-3 py-1 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
                    >
                      执行
                    </button>
                  )}
                </div>
                {task.status === 'processing' && (
                  <ProgressBar progress={task.progress} />
                )}
                {task.status === 'completed' && (
                  <Link
                    to={`/audit/${id}/result/${task.id}`}
                    className="text-blue-600 hover:text-blue-800 text-sm"
                  >
                    查看结果 →
                  </Link>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Quick Result View */}
      {completedTask && (
        <AuditResultCard taskId={completedTask.id} />
      )}
    </div>
  )
}

function AuditResultCard({ taskId }: { taskId: string }) {
  const { data: result, isLoading } = useQuery({
    queryKey: ['audit-result', taskId],
    queryFn: () => auditTaskApi.getResult(taskId),
    enabled: !!taskId,
  })

  if (isLoading) return <LoadingSpinner />
  if (!result) return null

  return (
    <div className="bg-white shadow rounded-lg">
      <div className="px-6 py-4 border-b flex justify-between items-center">
        <h2 className="text-lg font-semibold">审核结果</h2>
        <Link
          to={`/audit/result/${taskId}`}
          className="text-blue-600 hover:text-blue-800 text-sm"
        >
          查看完整报告 →
        </Link>
      </div>
      <div className="p-6">
        {/* Summary */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <div className="text-center p-4 bg-gray-50 rounded-lg">
            <div className="text-2xl font-bold text-gray-900">{result.summary.total_clauses}</div>
            <div className="text-sm text-gray-500">总条款数</div>
          </div>
          <div className="text-center p-4 bg-gray-50 rounded-lg">
            <div className="text-2xl font-bold text-red-600">{result.summary.issues_count}</div>
            <div className="text-sm text-gray-500">发现问题</div>
          </div>
          <div className="text-center p-4 bg-gray-50 rounded-lg">
            <div className="text-2xl font-bold text-orange-600">{result.summary.high_severity}</div>
            <div className="text-sm text-gray-500">高严重</div>
          </div>
          <div className="text-center p-4 bg-gray-50 rounded-lg">
            <div className="text-2xl font-bold text-yellow-600">{result.summary.medium_severity}</div>
            <div className="text-sm text-gray-500">中严重</div>
          </div>
        </div>

        {/* Issues Preview */}
        {result.issues.length > 0 ? (
          <div className="space-y-3">
            {result.issues.slice(0, 5).map((issue: { id: number; severity: string; type: string; clause_number?: string; description: string }) => (
              <div key={issue.id} className="border rounded-lg p-4">
                <div className="flex items-center space-x-2 mb-2">
                  <StatusBadge status={issue.severity} />
                  <span className="text-sm text-gray-500">
                    {issue.type === 'compliance' ? '合规性' : issue.type === 'completeness' ? '完整性' : '一致性'}问题
                  </span>
                  {issue.clause_number && (
                    <span className="text-sm text-gray-500">条款 {issue.clause_number}</span>
                  )}
                </div>
                <p className="text-sm text-gray-700">{issue.description}</p>
              </div>
            ))}
            {result.issues.length > 5 && (
              <div className="text-center text-sm text-gray-500">
                还有 {result.issues.length - 5} 个问题...
              </div>
            )}
          </div>
        ) : (
          <div className="text-center py-8 text-green-600">
            <div className="text-4xl mb-2">✓</div>
            <div>未发现问题，文档符合标准要求</div>
          </div>
        )}
      </div>
    </div>
  )
}
