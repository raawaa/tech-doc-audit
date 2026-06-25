import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Upload, FileText, Trash2, Play, Eye, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { auditDocApi, auditTaskApi, kbApi } from '../api/endpoints'
import type { AuditDocument } from '../api/types'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'
import { Modal } from '../components/Modal'
import { ProgressBar } from '../components/ProgressBar'

const statusActions: Record<string, { label: string; action: 'process' | 'audit' }> = {
  uploaded: { label: '解析', action: 'process' },
  parsed: { label: '解析', action: 'process' },
  indexed: { label: '审核', action: 'audit' },
  completed: { label: '审核', action: 'audit' },
}

export function AuditDashboard() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showAuditModal, setShowAuditModal] = useState(false)
  const [auditTarget, setAuditTarget] = useState<AuditDocument | null>(null)
  const [selectedKBs, setSelectedKBs] = useState<string[]>([])
  const [isDragging, setIsDragging] = useState(false)

  const { data: docs = [], isLoading } = useQuery({
    queryKey: ['audit-docs'],
    queryFn: () => auditDocApi.list(),
    refetchInterval: 3000,
  })

  const { data: kbs = [] } = useQuery({
    queryKey: ['kbs'],
    queryFn: () => kbApi.list(),
  })

  // 轮询所有任务，用于在仪表盘显示进度
  const { data: allTasks = [] } = useQuery({
    queryKey: ['audit-tasks-all'],
    queryFn: () => auditTaskApi.list(),
    refetchInterval: 3000,
  })

  // 建立 doc_id → 进度信息的映射
  const taskProgressMap = useMemo(() => {
    const map = new Map<string, { progress: number; label?: string }>()
    for (const task of allTasks) {
      if (task.status === 'processing') {
        map.set(task.document_id, { progress: task.progress, label: task.progress_label })
      }
    }
    return map
  }, [allTasks])

  const upload = useMutation({
    mutationFn: (file: File) => auditDocApi.upload(file),
    onSuccess: (doc) => {
      qc.invalidateQueries({ queryKey: ['audit-docs'] })
      if (doc?.id) processDoc.mutate(doc.id)
    },
  })

  const processDoc = useMutation({
    mutationFn: (id: string) => auditDocApi.process(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audit-docs'] }),
  })

  const deleteDoc = useMutation({
    mutationFn: (id: string) => auditDocApi.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audit-docs'] }),
  })

  const createAudit = useMutation({
    mutationFn: (data: { document_id: string; kb_ids: string[] }) => auditTaskApi.create(data),
    onSuccess: async (task) => {
      setShowAuditModal(false)
      setSelectedKBs([])
      toast.success('审核任务已创建')
      // 立即启动审核，导航到详情页时任务已是 processing，不会闪现「执行」按钮
      auditTaskApi.run(task.id, true)
      navigate(`/audit/${task.document_id}`)
      qc.invalidateQueries({ queryKey: ['audit-docs'] })
      qc.invalidateQueries({ queryKey: ['audit-tasks'] })
    },
    onError: (err) => {
      toast.error('创建审核任务失败：' + (err as Error).message)
    },
  })

  const handleUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) upload.mutate(file)
    e.target.value = ''
  }

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
    const file = e.dataTransfer.files?.[0]
    if (file) upload.mutate(file)
  }

  const handleAction = (doc: AuditDocument) => {
    const act = statusActions[doc.status]
    if (!act) return
    if (act.action === 'process') {
      processDoc.mutate(doc.id)
    } else {
      setAuditTarget(doc)
      setShowAuditModal(true)
    }
  }

  const startAudit = () => {
    if (!auditTarget || selectedKBs.length === 0) return
    createAudit.mutate({ document_id: auditTarget.id, kb_ids: selectedKBs })
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-900">文档审核</h1>
        <p className="mt-1 text-sm text-slate-500">上传招标文件等技术文档，对照知识库标准进行合规审核</p>
      </div>

      <Card>
        <CardBody>
          <label
            className={`flex flex-col items-center justify-center gap-3 py-8 cursor-pointer rounded-lg border-2 border-dashed transition-colors ${
              isDragging
                ? 'border-blue-500 bg-blue-50'
                : 'border-slate-200 hover:border-blue-400 hover:bg-blue-50/30'
            }`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            <Upload className={`w-8 h-8 ${isDragging ? 'text-blue-500' : 'text-slate-400'}`} />
            <div className="text-center">
              <p className="text-sm font-medium text-slate-700">
                {isDragging ? '释放文件以上传' : '点击或拖拽文件上传'}
              </p>
              <p className="text-xs text-slate-400 mt-0.5">支持 PDF、DOC、DOCX 格式</p>
            </div>
            <input type="file" accept=".pdf,.doc,.docx" onChange={handleUpload} className="hidden" />
          </label>
          {upload.isPending && (
            <div className="mt-3 flex items-center gap-2 text-sm text-blue-600">
              <Loader2 className="w-4 h-4 animate-spin" /> 上传中…
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="审核记录" />
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-12"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
          ) : docs.length === 0 ? (
            <div className="text-center py-12 text-slate-400 text-sm">暂无审核记录，请上传文档</div>
          ) : (
            <table className="w-full">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-slate-100">
                  <th className="text-left font-medium px-5 py-3">文档名称</th>
                  <th className="text-left font-medium px-5 py-3 w-16">类型</th>
                  <th className="text-left font-medium px-5 py-3 w-14">页数</th>
                  <th className="text-left font-medium px-5 py-3 w-24">状态</th>
                  <th className="text-right font-medium px-5 py-3 w-36">操作</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((doc) => (
                  <tr key={doc.id} className="border-b border-slate-50 hover:bg-slate-50/50 transition-colors">
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2.5">
                        <FileText className="w-4 h-4 text-slate-400 shrink-0" />
                        <span className="text-sm font-medium text-slate-900 truncate max-w-[320px]">
                          {doc.original_name || doc.name}
                        </span>
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-sm text-slate-500">{doc.file_type?.toUpperCase()}</td>
                    <td className="px-5 py-3.5 text-sm text-slate-500">{doc.page_count ?? '-'}</td>
                    <td className="px-5 py-3.5">
                      <div className="flex flex-col gap-1.5">
                        <Badge value={doc.status} />
                        {(() => {
                          const p = taskProgressMap.get(doc.id)
                          return p ? (
                            <div className="flex flex-col gap-0.5 min-w-[80px]">
                              <ProgressBar value={p.progress} className="h-1.5" indeterminate={p.progress >= 0.1 && p.progress < 0.9} />
                              <span className="text-[10px] text-slate-400 leading-tight">
                                {p.label ?? `处理中 ${Math.round(p.progress * 100)}%`}
                              </span>
                            </div>
                          ) : null
                        })()}
                      </div>
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center justify-end gap-1">
                        {statusActions[doc.status] && (
                          <button className="btn-ghost btn-sm" onClick={() => handleAction(doc)}>
                            {statusActions[doc.status].action === 'process' ? (
                              processDoc.isPending
                                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> 解析</>
                                : <><FileText className="w-3.5 h-3.5" /> 解析</>
                            ) : (
                              <><Play className="w-3.5 h-3.5" /> 审核</>
                            )}
                          </button>
                        )}
                        <button className="btn-ghost btn-sm" onClick={() => navigate(`/audit/${doc.id}`)}>
                          <Eye className="w-3.5 h-3.5" />
                        </button>
                        <button className="btn-ghost btn-sm !text-red-500 hover:!text-red-600" onClick={() => deleteDoc.mutate(doc.id)}>
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>

      <Modal open={showAuditModal} onClose={() => setShowAuditModal(false)} title="选择审核依据的知识库" wide>
        <div className="space-y-4">
          <p className="text-sm text-slate-500">选择用于对照审核的知识库（可多选）</p>
          <div className="space-y-2 max-h-60 overflow-y-auto">
            {kbs.map((kb) => (
              <label key={kb.id} className={`flex items-center gap-3 p-3 rounded-md border cursor-pointer transition-colors ${
                selectedKBs.includes(kb.id) ? 'border-blue-300 bg-blue-50/50' : 'border-slate-200 hover:border-slate-300'
              }`}>
                <input type="checkbox" checked={selectedKBs.includes(kb.id)}
                  onChange={() => setSelectedKBs(prev =>
                    prev.includes(kb.id) ? prev.filter(id => id !== kb.id) : [...prev, kb.id]
                  )}
                  className="rounded border-slate-300 text-blue-600 focus:ring-blue-500" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-900">{kb.name}</p>
                  <p className="text-xs text-slate-400 truncate">{kb.description || '-'}</p>
                </div>
                <Badge value={kb.category} />
              </label>
            ))}
          </div>
          {kbs.length === 0 && <p className="text-sm text-slate-400 text-center py-4">暂无知识库，请先创建</p>}
          <div className="flex justify-end gap-2 pt-2">
            <button className="btn-secondary" onClick={() => setShowAuditModal(false)}>取消</button>
            <button className="btn-primary" disabled={selectedKBs.length === 0 || createAudit.isPending} onClick={startAudit}>
              {createAudit.isPending ? '创建中…' : '开始审核'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
