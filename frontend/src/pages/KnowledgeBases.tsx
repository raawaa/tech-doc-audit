import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, BookOpen, Trash2, ChevronRight, Loader2 } from 'lucide-react'
import { kbApi } from '../api/endpoints'
import { Card, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'
import { Modal } from '../components/Modal'

export function KnowledgeBases() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [category, setCategory] = useState<'national' | 'industry' | 'enterprise'>('industry')

  const { data: kbs = [], isLoading } = useQuery({
    queryKey: ['kbs'],
    queryFn: () => kbApi.list(),
  })

  const create = useMutation({
    mutationFn: () => kbApi.create({ name, description: desc, category }),
    onSuccess: (kb) => {
      setShowCreate(false)
      setName(''); setDesc(''); setCategory('industry')
      qc.invalidateQueries({ queryKey: ['kbs'] })
      navigate(`/knowledge-bases/${kb.id}`)
    },
  })

  const del = useMutation({
    mutationFn: (id: string) => kbApi.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kbs'] }),
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-900">知识库</h1>
          <p className="mt-1 text-sm text-slate-500">管理技术标准、规范文档，作为审核依据</p>
        </div>
        <button className="btn-primary" onClick={() => setShowCreate(true)}>
          <Plus className="w-4 h-4" /> 创建知识库
        </button>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
      ) : kbs.length === 0 ? (
        <Card>
          <CardBody>
            <div className="text-center py-12">
              <BookOpen className="w-10 h-10 text-slate-300 mx-auto mb-3" />
              <p className="text-sm text-slate-500">暂无知识库，点击上方按钮创建</p>
            </div>
          </CardBody>
        </Card>
      ) : (
        <div className="grid gap-4">
          {kbs.map((kb) => (
            <Card key={kb.id} className="hover:shadow-md transition-shadow cursor-pointer" onClick={() => navigate(`/knowledge-bases/${kb.id}`)}>
              <div className="flex items-center justify-between p-5">
                <div className="flex items-center gap-4 min-w-0">
                  <div className="w-10 h-10 rounded-lg bg-blue-50 flex items-center justify-center shrink-0">
                    <BookOpen className="w-5 h-5 text-blue-600" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">{kb.name}</p>
                    <p className="text-xs text-slate-400 mt-0.5 truncate">{kb.description || '-'}</p>
                    <div className="flex items-center gap-3 mt-1.5 text-xs text-slate-400">
                      <span>{kb.document_count} 篇文档</span>
                      <span><Badge value={kb.category} /></span>
                      <span><Badge value={kb.index_status} /></span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button className="btn-ghost btn-sm !text-red-500 hover:!text-red-600" onClick={(e) => { e.stopPropagation(); del.mutate(kb.id) }}>
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  <ChevronRight className="w-4 h-4 text-slate-300" />
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="创建知识库">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">名称</label>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="知识库名称" />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">描述</label>
            <input className="input" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="简要描述" />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">分类</label>
            <select className="input" value={category} onChange={(e) => setCategory(e.target.value as typeof category)}>
              <option value="national">国家标准</option>
              <option value="industry">行业标准</option>
              <option value="enterprise">企业标准</option>
            </select>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button className="btn-secondary" onClick={() => setShowCreate(false)}>取消</button>
            <button className="btn-primary" disabled={!name || create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? '创建中…' : '创建'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
