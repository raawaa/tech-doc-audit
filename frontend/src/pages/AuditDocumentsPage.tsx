import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { auditDocApi, auditTaskApi, kbApi } from '@/api'
import { StatusBadge, EmptyState, LoadingSpinner } from '@/components/StatusBadge'

export function AuditDocumentsPage() {
  const queryClient = useQueryClient()
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [showAuditModal, setShowAuditModal] = useState<string | null>(null)
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([])
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)

  const { data: docs = [], isLoading } = useQuery({
    queryKey: ['audit-documents'],
    queryFn: () => auditDocApi.list(),
  })

  const { data: kbs = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () => kbApi.list(),
  })

  const uploadMutation = useMutation({
    mutationFn: auditDocApi.upload,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-documents'] })
      setSelectedFile(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: auditDocApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-documents'] })
    },
  })

  const processMutation = useMutation({
    mutationFn: auditDocApi.process,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-documents'] })
    },
  })

  const startAuditMutation = useMutation({
    mutationFn: ({ docId, kbIds }: { docId: string; kbIds: string[] }) =>
      auditTaskApi.create({ document_id: docId, kb_ids: kbIds }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-tasks'] })
      setShowAuditModal(null)
    },
  })

  const openAuditModal = (docId: string) => {
    setSelectedDocId(docId)
    setSelectedKbIds(kbs.filter((k) => k.index_status === 'ready').map((k) => k.id))
    setShowAuditModal(docId)
  }

  if (isLoading) return <LoadingSpinner />

  return (
    <div>
      <div className="sm:flex sm:items-center sm:justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">待审核文档</h1>
      </div>

      {/* Upload Section */}
      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <h2 className="text-lg font-semibold mb-4">上传文档</h2>
        <div className="flex items-center space-x-4">
          <input
            type="file"
            accept=".pdf,.doc,.docx"
            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
          />
          <button
            onClick={() => selectedFile && uploadMutation.mutate(selectedFile)}
            disabled={!selectedFile || uploadMutation.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm disabled:opacity-50"
          >
            {uploadMutation.isPending ? '上传中...' : '上传'}
          </button>
        </div>
      </div>

      {/* Documents List */}
      {docs.length === 0 ? (
        <EmptyState
          title="暂无待审核文档"
          description="上传一个文档开始审核流程"
        />
      ) : (
        <div className="bg-white shadow overflow-hidden sm:rounded-md">
          <ul className="divide-y divide-gray-200">
            {docs.map((doc) => (
              <li key={doc.id}>
                <div className="px-4 py-4 sm:px-6 flex items-center justify-between">
                  <div>
                    <p className="font-medium text-gray-900">{doc.name}</p>
                    <p className="text-sm text-gray-500">
                      {doc.file_type.toUpperCase()} | {doc.page_count ? `${doc.page_count} 页` : '页数未知'} |{' '}
                      {new Date(doc.created_at).toLocaleString()}
                    </p>
                  </div>
                  <div className="flex items-center space-x-2">
                    <StatusBadge status={doc.status} />
                    {doc.status === 'uploaded' && (
                      <button
                        onClick={() => processMutation.mutate(doc.id)}
                        disabled={processMutation.isPending}
                        className="px-3 py-1 text-sm border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50"
                      >
                        解析
                      </button>
                    )}
                    {(doc.status === 'indexed' || doc.status === 'completed') && (
                      <button
                        onClick={() => openAuditModal(doc.id)}
                        className="px-3 py-1 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700"
                      >
                        审核
                      </button>
                    )}
                    <Link
                      to={`/audit/${doc.id}`}
                      className="px-3 py-1 text-sm text-blue-600 hover:text-blue-800"
                    >
                      详情
                    </Link>
                    <button
                      onClick={() => {
                        if (confirm('确定要删除此文档吗？')) deleteMutation.mutate(doc.id)
                      }}
                      className="px-3 py-1 text-sm text-red-600 hover:text-red-800"
                    >
                      删除
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Audit Modal */}
      {showAuditModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h2 className="text-lg font-semibold mb-4">选择审核知识库</h2>
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {kbs.map((kb) => (
                <label key={kb.id} className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    checked={selectedKbIds.includes(kb.id)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedKbIds([...selectedKbIds, kb.id])
                      } else {
                        setSelectedKbIds(selectedKbIds.filter((id) => id !== kb.id))
                      }
                    }}
                    className="rounded border-gray-300"
                  />
                  <span className="text-sm">
                    {kb.name}
                    {kb.index_status !== 'ready' && (
                      <span className="text-gray-400 text-xs ml-2">(索引未就绪)</span>
                    )}
                  </span>
                </label>
              ))}
            </div>
            <div className="mt-6 flex justify-end space-x-3">
              <button
                onClick={() => setShowAuditModal(null)}
                className="px-4 py-2 border border-gray-300 rounded-md text-sm"
              >
                取消
              </button>
              <button
                onClick={() => selectedDocId && startAuditMutation.mutate({ docId: selectedDocId, kbIds: selectedKbIds })}
                disabled={selectedKbIds.length === 0 || startAuditMutation.isPending}
                className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm disabled:opacity-50"
              >
                {startAuditMutation.isPending ? '创建中...' : '开始审核'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
