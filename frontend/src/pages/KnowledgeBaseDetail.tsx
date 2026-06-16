import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Upload, FileText, RefreshCw, Trash2, Loader2 } from 'lucide-react'
import { kbApi } from '../api/endpoints'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'

export function KnowledgeBaseDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: kb, isLoading } = useQuery({
    queryKey: ['kb', id],
    queryFn: () => kbApi.get(id!),
    enabled: !!id,
    // 索引重建中时每 2 秒轮询进度
    refetchInterval: (query) =>
      query.state.data?.index_status === 'building' ? 2000 : false,
  })

  const { data: docs = [], isLoading: docsLoading } = useQuery({
    queryKey: ['kb-docs', id],
    queryFn: () => kbApi.documents.list(id!),
    enabled: !!id,
  })

  const importDoc = useMutation({
    mutationFn: (file: File) => kbApi.documents.import(id!, file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
      qc.invalidateQueries({ queryKey: ['kb', id] })
    },
  })

  const batchImport = useMutation({
    mutationFn: (files: File[]) => kbApi.documents.batchImport(id!, files),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
      qc.invalidateQueries({ queryKey: ['kb', id] })
    },
    onError: (err) => console.error('批量上传失败:', err),
  })

  const reindex = useMutation({
    mutationFn: () => kbApi.reindex(id!),
    // 成功后由 refetchInterval 轮询自动获取进度，无需手动 invalidate
  })

  const handleFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length === 0) return
    e.target.value = ''
    if (files.length === 1) {
      importDoc.mutate(files[0])
    } else {
      batchImport.mutate(files)
    }
  }

  const deleteDoc = useMutation({
    mutationFn: (docId: string) => kbApi.documents.delete(id!, docId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kb-docs', id] }),
  })

  if (isLoading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (!kb) return <div className="text-center py-20 text-slate-500">知识库不存在</div>

  return (
    <div className="space-y-6">
      <button className="btn-ghost btn-sm -ml-2" onClick={() => navigate('/knowledge-bases')}>
        <ArrowLeft className="w-4 h-4" /> 返回
      </button>

      <Card>
        <CardHeader title="基本信息" action={
          <button className="btn-secondary btn-sm" onClick={() => reindex.mutate()} disabled={kb.index_status === 'building'}>
            <RefreshCw className={`w-3.5 h-3.5 ${kb.index_status === 'building' ? 'animate-spin' : ''}`} />
            {kb.index_status === 'building' ? '索引中…' : '重建索引'}
          </button>
        } />
        <CardBody>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div><span className="text-slate-500">名称</span><p className="font-medium mt-0.5">{kb.name}</p></div>
            <div><span className="text-slate-500">分类</span><p className="mt-0.5"><Badge value={kb.category} /></p></div>
            <div><span className="text-slate-500">描述</span><p className="font-medium mt-0.5">{kb.description || '-'}</p></div>
            <div><span className="text-slate-500">索引状态</span><p className="mt-0.5"><Badge value={kb.index_status} /></p></div>
            <div><span className="text-slate-500">文档数</span><p className="font-medium mt-0.5">{kb.document_count}</p></div>
          </div>

          {/* 索引重建进度 */}
          {kb.index_status === 'building' && (
            <div className="mt-4 pt-4 border-t border-slate-100">
              <div className="flex items-center justify-between text-xs text-slate-500 mb-2">
                <span>
                  <Loader2 className="w-3 h-3 inline animate-spin mr-1" />
                  正在索引：{kb.index_current_doc || '准备中…'}
                </span>
                <span>{Math.round((kb.index_progress ?? 0) * 100)}%</span>
              </div>
              <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-600 rounded-full transition-all duration-500"
                  style={{ width: `${(kb.index_progress ?? 0) * 100}%` }}
                />
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="文档管理" action={
          <div className="flex items-center gap-2">
            <label className="btn-secondary btn-sm cursor-pointer">
              <Upload className="w-3.5 h-3.5" /> 导入文档
              <input type="file" accept=".pdf,.doc,.docx,.md" multiple className="hidden" onChange={handleFiles} />
            </label>
            {batchImport.isPending && (
              <span className="text-xs text-blue-600">上传中 ({batchImport.variables?.length || 0} 个文件)…</span>
            )}
          </div>
        } />
        <CardBody className="p-0">
          {docsLoading ? (
            <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-slate-400" /></div>
          ) : docs.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">暂无文档，点击"导入文档"添加</div>
          ) : (
            <table className="w-full">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-slate-100">
                  <th className="text-left font-medium px-5 py-3">名称</th>
                  <th className="text-left font-medium px-5 py-3 w-20">类型</th>
                  <th className="text-left font-medium px-5 py-3 w-20">页数</th>
                  <th className="text-left font-medium px-5 py-3 w-24">索引状态</th>
                  <th className="text-right font-medium px-5 py-3 w-16">操作</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id} className="border-b border-slate-50 hover:bg-slate-50/50">
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2.5">
                        <FileText className="w-4 h-4 text-slate-400 shrink-0" />
                        <span className="text-sm text-slate-900 truncate max-w-[300px]">{d.original_name || d.name}</span>
                      </div>
                    </td>
                    <td className="px-5 py-3 text-sm text-slate-500">{d.file_type?.toUpperCase()}</td>
                    <td className="px-5 py-3 text-sm text-slate-500">{d.page_count ?? '-'}</td>
                    <td className="px-5 py-3"><Badge value={d.index_status} /></td>
                    <td className="px-5 py-3 text-right">
                      <button className="btn-ghost btn-sm !text-red-500 hover:!text-red-600" onClick={() => deleteDoc.mutate(d.id)}>
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>
    </div>
  )
}
