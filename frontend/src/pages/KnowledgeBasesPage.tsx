import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { kbApi } from '@/api'
import { StatusBadge, EmptyState, LoadingSpinner } from '@/components/StatusBadge'

export function KnowledgeBasesPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newCategory, setNewCategory] = useState<'national' | 'industry' | 'enterprise'>('national')

  const { data: kbs = [], isLoading } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () => kbApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: kbApi.create,
    onSuccess: (newKb) => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      setShowCreate(false)
      setNewName('')
      // 创建成功后跳转到详情页
      navigate(`/knowledge-bases/${newKb.id}`)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: kbApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
    },
  })

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) return
    createMutation.mutate({ name: newName, category: newCategory })
  }

  if (isLoading) return <LoadingSpinner />

  return (
    <div>
      <div className="sm:flex sm:items-center sm:justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">知识库</h1>
        <button
          onClick={() => setShowCreate(true)}
          className="mt-4 sm:mt-0 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-blue-600 hover:bg-blue-700"
        >
          创建知识库
        </button>
      </div>

      {/* Create Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h2 className="text-lg font-semibold mb-4">创建知识库</h2>
            <form onSubmit={handleCreate}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700">名称</label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm px-3 py-2"
                    placeholder="知识库名称"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">分类</label>
                  <select
                    value={newCategory}
                    onChange={(e) => setNewCategory(e.target.value as typeof newCategory)}
                    className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm px-3 py-2"
                  >
                    <option value="national">国家标准</option>
                    <option value="industry">行业标准</option>
                    <option value="enterprise">企业规范</option>
                  </select>
                </div>
              </div>
              <div className="mt-6 flex justify-end space-x-3">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="px-4 py-2 border border-gray-300 rounded-md text-sm"
                >
                  取消
                </button>
                <button
                  type="submit"
                  disabled={createMutation.isPending}
                  className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm disabled:opacity-50"
                >
                  {createMutation.isPending ? '创建中...' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* List */}
      {kbs.length === 0 ? (
        <EmptyState
          title="暂无知识库"
          description="创建一个知识库来管理您的标准文档"
        />
      ) : (
        <div className="bg-white shadow overflow-hidden sm:rounded-md">
          <ul className="divide-y divide-gray-200">
            {kbs.map((kb) => (
              <li key={kb.id}>
                <div className="px-4 py-4 sm:px-6 flex items-center justify-between">
                  <div>
                    <Link to={`/knowledge-bases/${kb.id}`} className="text-lg font-medium text-blue-600 hover:text-blue-800">
                      {kb.name}
                    </Link>
                    <p className="mt-1 text-sm text-gray-500">{kb.description || '暂无描述'}</p>
                    <div className="mt-2 flex items-center space-x-4 text-sm text-gray-500">
                      <span>文档: {kb.document_count}</span>
                      <span>分类: {kb.category === 'national' ? '国家标准' : kb.category === 'industry' ? '行业标准' : '企业规范'}</span>
                    </div>
                  </div>
                  <div className="flex items-center space-x-4">
                    <StatusBadge status={kb.index_status} />
                    <button
                      onClick={() => {
                        if (confirm('确定要删除此知识库吗？')) deleteMutation.mutate(kb.id)
                      }}
                      className="text-red-600 hover:text-red-800 text-sm"
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
    </div>
  )
}
