import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Play, Loader2, FileText } from 'lucide-react'
import { auditDocApi, auditTaskApi } from '../api/endpoints'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'

export function AuditDocDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: doc, isLoading } = useQuery({
    queryKey: ['audit-doc', id],
    queryFn: () => auditDocApi.get(id!),
    enabled: !!id,
  })

  const { data: tasks = [], isLoading: tasksLoading } = useQuery({
    queryKey: ['audit-tasks', id],
    queryFn: () => auditTaskApi.list(id),
    enabled: !!id,
    refetchInterval: 2000,
  })

  const processDoc = useMutation({
    mutationFn: () => auditDocApi.process(id!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audit-doc', id] }),
  })

  const runTask = useMutation({
    mutationFn: (taskId: string) => auditTaskApi.run(taskId, true),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audit-tasks', id] }),
  })

  if (isLoading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (!doc) return <div className="text-center py-20 text-slate-500">文档不存在</div>

  return (
    <div className="space-y-6">
      <button className="btn-ghost btn-sm -ml-2" onClick={() => navigate('/audit')}>
        <ArrowLeft className="w-4 h-4" /> 返回
      </button>

      <Card>
        <CardHeader title="文档信息" />
        <CardBody>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div><span className="text-slate-500">名称</span><p className="font-medium text-slate-900 mt-0.5">{doc.original_name || doc.name}</p></div>
            <div><span className="text-slate-500">格式</span><p className="font-medium text-slate-900 mt-0.5">{doc.file_type?.toUpperCase()}</p></div>
            <div><span className="text-slate-500">页数</span><p className="font-medium text-slate-900 mt-0.5">{doc.page_count ?? '-'}</p></div>
            <div><span className="text-slate-500">状态</span><p className="mt-0.5"><Badge value={doc.status} /></p></div>
            <div><span className="text-slate-500">上传时间</span><p className="font-medium text-slate-900 mt-0.5">{new Date(doc.created_at).toLocaleString('zh-CN')}</p></div>
          </div>
          {doc.status === 'uploaded' && (
            <button className="btn-primary mt-4" onClick={() => processDoc.mutate()} disabled={processDoc.isPending}>
              {processDoc.isPending ? <><Loader2 className="w-4 h-4 animate-spin" /> 解析中…</> : '解析文档'}
            </button>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="审核任务" />
        <CardBody className="p-0">
          {tasksLoading ? (
            <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-slate-400" /></div>
          ) : tasks.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">暂无审核任务</div>
          ) : (
            <div className="divide-y divide-slate-100">
              {tasks.map((task) => (
                <div key={task.id} className="px-5 py-4 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <FileText className="w-4 h-4 text-slate-400" />
                    <div>
                      <p className="text-sm font-medium text-slate-900">
                        审核任务
                        <span className="ml-2"><Badge value={task.status} /></span>
                      </p>
                      <p className="text-xs text-slate-400 mt-0.5">
                        {new Date(task.created_at).toLocaleString('zh-CN')}
                        {task.status === 'processing' && ` · ${Math.round(task.progress * 100)}%`}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {task.status === 'pending' && (
                      <button className="btn-primary btn-sm" onClick={() => runTask.mutate(task.id)}>
                        <Play className="w-3.5 h-3.5" /> 执行
                      </button>
                    )}
                    {task.status === 'completed' && (
                      <button className="btn-secondary btn-sm" onClick={() => navigate(`/audit/${id}/result/${task.id}`)}>
                        查看结果
                      </button>
                    )}
                    {task.status === 'processing' && task.result?.issues && (
                      <span className="text-xs text-slate-400">已发现 {task.result.issues.length} 个问题</span>
                    )}
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
