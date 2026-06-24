import { useRef, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Play, Loader2, FileText, XCircle, ChevronDown, ChevronRight } from 'lucide-react'
import { toast } from 'sonner'
import { auditDocApi, auditTaskApi } from '../api/endpoints'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'
import { ProgressBar } from '../components/ProgressBar'
import { AuditStream } from '../components/AuditStream'

export function AuditDocDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const prevTaskStatus = useRef<Record<string, string>>({})
  const [streamingTaskId, setStreamingTaskId] = useState<string | null>(null)

  const { data: doc, isLoading } = useQuery({
    queryKey: ['audit-doc', id],
    queryFn: () => auditDocApi.get(id!),
    enabled: !!id,
  })

  const { data: tasks = [], isLoading: tasksLoading } = useQuery({
    queryKey: ['audit-tasks', id],
    queryFn: () => auditTaskApi.list(id),
    enabled: !!id,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      // 有活跃任务时继续轮询，否则停止
      if (data.some(t => t.status === 'processing' || t.status === 'pending')) return 2000
      return false
    },
  })

  // 检测任务状态变更 → toast 通知
  useEffect(() => {
    for (const task of tasks) {
      const prev = prevTaskStatus.current[task.id]
      if (!prev) {
        prevTaskStatus.current[task.id] = task.status
        continue
      }
      if (prev === 'processing' && task.status === 'completed') {
        toast.success('审核完成！', {
          action: {
            label: '查看结果',
            onClick: () => navigate(`/audit/${id}/result/${task.id}`),
          },
        })
      } else if (prev === 'processing' && task.status === 'failed') {
        toast.error('审核失败')
      }
      prevTaskStatus.current[task.id] = task.status
    }
  }, [tasks, id, navigate])

  // 自动检测 processing 状态的任务，开启流式展示
  useEffect(() => {
    const processingTask = tasks.find(t => t.status === 'processing')
    if (processingTask && !streamingTaskId) {
      setStreamingTaskId(processingTask.id)
    }
  }, [tasks, streamingTaskId])

  const processDoc = useMutation({
    mutationFn: () => auditDocApi.process(id!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audit-doc', id] }),
  })

  // 文档结构（文档解析后才展示）
  const [structureOpen, setStructureOpen] = useState(false)
  const { data: structure } = useQuery({
    queryKey: ['doc-structure', id],
    queryFn: () => auditDocApi.getStructure(id!),
    enabled: !!id && doc?.status !== 'uploaded',
  })

  const runTask = useMutation({
    mutationFn: (taskId: string) => auditTaskApi.run(taskId, true),
    onSuccess: (_data, taskId) => {
      setStreamingTaskId(taskId)
      qc.invalidateQueries({ queryKey: ['audit-tasks', id] })
    },
  })

  const cancelTask = useMutation({
    mutationFn: (taskId: string) => auditTaskApi.cancel(taskId),
    onSuccess: () => {
      toast.success('任务已取消')
      qc.invalidateQueries({ queryKey: ['audit-tasks', id] })
    },
    onError: (err) => {
      toast.error('取消失败：' + (err as Error).message)
    },
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

      {/* 文档结构 */}
      {structure && structure.chapters.length > 0 && (
        <Card>
          <CardHeader
            title={`文档结构（${structure.total_clauses} 条款）`}
            action={
              <button className="btn-ghost btn-sm" onClick={() => setStructureOpen(o => !o)}>
                {structureOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                {structureOpen ? '收起' : '展开'}
              </button>
            }
          />
          {structureOpen && (
            <CardBody className="p-0">
              <div className="max-h-80 overflow-y-auto">
                {structure.chapters.map((ch, i) => (
                  <div key={i} className="px-5 py-3 border-b border-slate-50 last:border-0">
                    <div className="flex items-center gap-2">
                      {ch.number && <span className="text-xs font-mono text-blue-600">{ch.number}</span>}
                      <span className="text-sm font-medium text-slate-800">{ch.title}</span>
                      {ch.clauses.length > 0 && (
                        <span className="text-xs text-slate-400">{ch.clauses.length} 条款</span>
                      )}
                    </div>
                    {ch.clauses.length > 0 && (
                      <div className="mt-1.5 ml-4 space-y-1">
                        {ch.clauses.map((c, j) => (
                          <div key={j} className="text-xs text-slate-500 flex gap-1.5">
                            <span className="font-mono text-slate-400 shrink-0">{c.number}</span>
                            <span className="truncate">{c.text}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </CardBody>
          )}
        </Card>
      )}

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
                    {task.status === 'processing' && (
                      <div className="flex items-center gap-2">
                        <div className="w-24">
                          <ProgressBar value={task.progress} />
                        </div>
                        <span className="text-xs text-slate-500 min-w-[4rem]">
                          {task.progress_label ?? `处理中 ${Math.round(task.progress * 100)}%`}
                        </span>
                        <button
                          className="btn-ghost btn-sm !text-red-400 hover:!text-red-600"
                          onClick={() => cancelTask.mutate(task.id)}
                          title="取消任务"
                        >
                          <XCircle className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    )}
                    {task.status === 'failed' && (
                      <button
                        className="btn-ghost btn-sm !text-red-500"
                        onClick={() => cancelTask.mutate(task.id)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardBody>
      </Card>

      {/* Agentic 审核流式面板 */}
      {streamingTaskId && (
        <AuditStream taskId={streamingTaskId} docId={doc.id} />
      )}
    </div>
  )
}
