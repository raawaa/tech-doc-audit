import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { kbApi } from '@/api'
import { StatusBadge, LoadingSpinner } from '@/components/StatusBadge'

export function KnowledgeBaseDetailPage() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const [selectedFile, setSelectedFile] = useState<File | null>(null)

  const { data: kb, isLoading } = useQuery({
    queryKey: ['knowledge-base', id],
    queryFn: () => kbApi.get(id!),
    enabled: !!id,
  })

  const { data: docs = [], isLoading: docsLoading } = useQuery({
    queryKey: ['knowledge-base-docs', id],
    queryFn: () => kbApi.listDocuments(id!),
    enabled: !!id,
  })

  const importMutation = useMutation({
    mutationFn: (file: File) => kbApi.importDocument(id!, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-base-docs', id] })
      setSelectedFile(null)
    },
  })

  const reindexMutation = useMutation({
    mutationFn: () => kbApi.reindex(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-base', id] })
    },
  })

  if (isLoading || docsLoading) return <LoadingSpinner />
  if (!kb) return <div>知识库不存在</div>

  return (
    <div>
      <div className="mb-6">
        <Link to="/knowledge-bases" className="text-blue-600 hover:text-blue-800 text-sm">
          ← 返回知识库列表
        </Link>
      </div>

      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <div className="flex justify-between items-start mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{kb.name}</h1>
            <p className="mt-1 text-gray-500">{kb.description || '暂无描述'}</p>
          </div>
          <div className="flex items-center space-x-2">
            <StatusBadge status={kb.index_status} />
            <button
              onClick={() => reindexMutation.mutate()}
              disabled={reindexMutation.isPending}
              className="px-3 py-1 text-sm border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
            >
              {reindexMutation.isPending ? '重建中...' : '重建索引'}
            </button>
          </div>
        </div>

        <div className="flex items-center space-x-4 text-sm text-gray-500">
          <span>分类: {kb.category === 'national' ? '国家标准' : kb.category === 'industry' ? '行业标准' : '企业规范'}</span>
          <span>文档数: {kb.document_count}</span>
          <span>创建时间: {new Date(kb.created_at).toLocaleDateString()}</span>
        </div>
      </div>

      {/* Import Section */}
      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <h2 className="text-lg font-semibold mb-4">导入文档</h2>
        <div className="flex items-center space-x-4">
          <input
            type="file"
            accept=".pdf,.doc,.docx"
            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
          />
          <button
            onClick={() => selectedFile && importMutation.mutate(selectedFile)}
            disabled={!selectedFile || importMutation.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm disabled:opacity-50"
          >
            {importMutation.isPending ? '导入中...' : '导入'}
          </button>
        </div>
      </div>

      {/* Documents List */}
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">文档列表 ({docs.length})</h2>
        </div>
        {docs.length === 0 ? (
          <div className="p-6 text-center text-gray-500">暂无文档</div>
        ) : (
          <ul className="divide-y divide-gray-200">
            {docs.map((doc) => (
              <li key={doc.id} className="px-6 py-4 flex items-center justify-between">
                <div>
                  <p className="font-medium text-gray-900">{doc.name}</p>
                  <p className="text-sm text-gray-500">
                    {doc.file_type.toUpperCase()} | {doc.page_count ? `${doc.page_count} 页` : '页数未知'}
                  </p>
                </div>
                <StatusBadge status={doc.index_status} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
