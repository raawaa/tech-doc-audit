import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ArrowLeft, Upload, FileText, RefreshCw, Trash2, Loader2, RotateCw, ListRestart } from 'lucide-react'
import { toast } from 'sonner'
import type { KBDocument, BulkReparsePreflight } from '../api/types'
import { kbApi } from '../api/endpoints'
import { Card, CardHeader, CardBody } from '../components/Card'
import { Badge } from '../components/Badge'
import { ProgressBar } from '../components/ProgressBar'
const INDEXING_STATUSES = ['pending_index', 'indexing'] as const
const isIndexingDoc = (d: KBDocument): boolean =>
  (INDEXING_STATUSES as readonly string[]).includes(d.embedding_status)

// 把预检结果铺成 ``window.confirm`` 文案 —— 沿用 per-doc reparse 的形态，
// 不引入新 UI 语言（spec #102 story 39）。数字必须来自预检，不能拍脑袋写。
// AC #112 明确要求四项：目标篇数 / 未命中 / 预计 OCR / 超限跳过；其余信息不在
// issue 授权范围内，不擅自加入对话框。
const buildBulkReparseConfirmMessage = (p: BulkReparsePreflight): string => {
  const lines: string[] = [
    `目标文档：${p.target_count} 篇`,
    `其中未命中缓存：${p.uncached_docs} 篇（将消耗约 ${p.estimated_ocr_pages} 页 OCR 配额）`,
  ]
  if (p.over_page_limit.length > 0) {
    lines.push(`超页数上限将被跳过：${p.over_page_limit.length} 篇`)
  }
  lines.push('')
  lines.push(p.target_count === 0 ? '当前没有待重新解析的文档。' : '确认开始批量重新解析？')
  return lines.join('\n')
}

export function KnowledgeBaseDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  // 预检请求不在 ``useMutation`` 里（mutation 只装"确认后那一下 POST"），
  // 这里独立一份状态，防止用户在预检 fetch 期间连点按钮导致两次 POST。
  const [preflightPending, setPreflightPending] = useState(false)

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
    // 当 KB 正在构建 或 有文档处于 pending_index / indexing 状态时持续轮询
    refetchInterval: (query) => {
      if (kb?.index_status === 'building') return 2000
      // KB 已 searchable 但仍有文档卡在 pending_index / indexing（部分文档还在向量化）
      const list = query.state.data
      if (list && list.some(isIndexingDoc)) {
        return 2000
      }
      return false
    },
  })

  const importDoc = useMutation({
    mutationFn: (file: File) => kbApi.documents.import(id!, file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
      qc.invalidateQueries({ queryKey: ['kb', id] })
    },
    onError: (err) => toast.error('导入失败：' + (err as Error).message),
  })

  const batchImport = useMutation({
    mutationFn: (files: File[]) => kbApi.documents.batchImport(id!, files),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
      qc.invalidateQueries({ queryKey: ['kb', id] })
    },
    onError: (err) => toast.error('批量导入失败：' + (err as Error).message),
  })

  const reindex = useMutation({
    mutationFn: () => kbApi.reindex(id!),
    onSuccess: () => {
      toast.success('索引重建已启动', { description: '正在后台重建，请稍候…' })
      // 手动 invalidate 触发立即刷新（而非等下一个轮询周期）
      qc.invalidateQueries({ queryKey: ['kb', id] })
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
    },
    onError: (err) => toast.error('重建索引失败：' + (err as Error).message),
  })

  // 批量重新解析（spec #102 / issue #112）—— 预检先行 → 二次确认 → 触发。
  // 互斥：``reindex`` / ``bulkReparse`` 通过 KB ``index_status`` 字段自然互斥（issue #111
  // 设计：同一字段守两边），UI 上多按一层 ``isPending`` 防止"POST 在飞、building 还没
  // 写到 KB"那一瞬被用户连点。
  const bulkReparse = useMutation({
    mutationFn: () => kbApi.bulkReparse.trigger(id!),
    onSuccess: (res) => {
      toast.success('批量重新解析已启动', {
        description: `共 ${res.target_count} 篇文档，请观察进度变化`,
      })
      qc.invalidateQueries({ queryKey: ['kb', id] })
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
    },
    onError: (err) => {
      // axios 拦截器把 ``err.response.data.detail`` 落到 message 上，这里用结构化
      // status 而非 substring 嗅探 —— 后端 message 文案一旦变动也不会静默失效。
      const e = err as Error & { response?: { status?: number } }
      if (e.response?.status === 409) {
        toast.error('已有索引构建任务在进行中，请等待完成后再试')
      } else {
        toast.error('批量重新解析失败：' + (e.message || '未知错误'))
      }
    },
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

  const reparseDoc = useMutation({
    mutationFn: async (docId: string) => {
      // 二次确认：明示会消耗 OCR 配额
      const ok = window.confirm('此操作将消耗 OCR 配额（仅当缓存未命中时），确认重新解析？')
      if (!ok) throw new Error('cancelled')
      return kbApi.documents.reparse(docId)
    },
    onSuccess: () => {
      toast.success('重新解析已启动', { description: '请观察文档索引状态变化' })
      qc.invalidateQueries({ queryKey: ['kb-docs', id] })
      qc.invalidateQueries({ queryKey: ['kb', id] })
    },
    onError: (err) => {
      if ((err as Error).message !== 'cancelled') {
        toast.error('重新解析失败：' + (err as Error).message)
      }
    },
  })

  // 批量重新解析入口：先拉预检（无副作用）→ 在确认对话框里把成本数字摊给用户
  // （spec #102 story 35）→ 确认后入队。空批次在客户端短路：服务端也会短路
  // （kb.router _build_preflight_payload 也返 target_count=0），前端直接吞掉
  // 这次点击，避免一次无意义的 POST / 一条多余 toast。
  const handleBulkReparse = async () => {
    if (preflightPending) return
    setPreflightPending(true)
    try {
      const preflight = await kbApi.bulkReparse.preflight(id!)
      if (preflight.target_count === 0) {
        toast.info('当前没有待重新解析的文档')
        return
      }
      const ok = window.confirm(buildBulkReparseConfirmMessage(preflight))
      if (!ok) return
      bulkReparse.mutate()
    } catch (err) {
      // axios 拦截器已把 ``err.response.data.detail`` 落到 message 上。
      const e = err as Error & { response?: { status?: number } }
      const detail = e.message || '未知错误'
      toast.error(e.response?.status === 404 ? '知识库不存在或已被删除' : `预检失败：${detail}`)
    } finally {
      setPreflightPending(false)
    }
  }

  if (isLoading) return <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
  if (!kb) return <div className="text-center py-20 text-slate-500">知识库不存在</div>

  return (
    <div className="space-y-6">
      <button className="btn-ghost btn-sm -ml-2" onClick={() => navigate('/knowledge-bases')}>
        <ArrowLeft className="w-4 h-4" /> 返回
      </button>

      <Card>
        <CardHeader title="基本信息" action={
          <div className="flex items-center gap-2">
            <button
              className="btn-secondary btn-sm"
              onClick={() => reindex.mutate()}
              disabled={kb.index_status === 'building' || reindex.isPending || bulkReparse.isPending || preflightPending}
              title="重建知识库的向量索引（不重新解析文档，不消耗 OCR 配额）"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${(kb.index_status === 'building' || reindex.isPending) ? 'animate-spin' : ''}`} />
              {kb.index_status === 'building' ? '索引中…' : reindex.isPending ? '启动中…' : '重建索引'}
            </button>
            <button
              className="btn-secondary btn-sm"
              onClick={handleBulkReparse}
              disabled={kb.index_status === 'building' || bulkReparse.isPending || preflightPending}
              title="对 KB 内全部待重新解析文档执行批量重新解析（消耗 OCR 配额，仅缓存未命中时）"
            >
              <ListRestart className={`w-3.5 h-3.5 ${(bulkReparse.isPending || preflightPending) ? 'animate-spin' : ''}`} />
              {preflightPending ? '预检中…' : bulkReparse.isPending ? '启动中…' : '批量重新解析'}
            </button>
          </div>
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
              <ProgressBar value={kb.index_progress ?? 0} />
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
                  <th className="text-right font-medium px-5 py-3 w-32">操作</th>
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
                    <td className="px-5 py-3"><Badge value={d.embedding_status} /></td>
                    <td className="px-5 py-3 text-right">
                      <div className="inline-flex items-center gap-1">
                        <button
                          className="btn-ghost btn-sm !text-blue-600 hover:!text-blue-700 disabled:!text-slate-300"
                          title="重新解析（PRD #29 V4）：重新走 PaddleOCR + 重建索引，仅当缓存未命中才消耗 OCR 配额"
                          onClick={() => reparseDoc.mutate(d.id)}
                          disabled={
                            isIndexingDoc(d)
                            || (reparseDoc.isPending && reparseDoc.variables === d.id)
                          }
                        >
                          <RotateCw className={`w-3.5 h-3.5 ${(isIndexingDoc(d) || (reparseDoc.isPending && reparseDoc.variables === d.id)) ? 'animate-spin' : ''}`} />
                        </button>
                        <button
                          className="btn-ghost btn-sm !text-red-500 hover:!text-red-600"
                          onClick={() => deleteDoc.mutate(d.id)}
                        >
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
    </div>
  )
}
