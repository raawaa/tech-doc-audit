import { useState, useEffect, useRef } from 'react'
import { kbApi, qaApi } from '../api/index'
import type { KnowledgeBase, QASource } from '../api/types'

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: QASource[]
}

export function QAPage() {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [selectedKbIds, setSelectedKbIds] = useState<Set<string>>(new Set())
  const [sessionId, setSessionId] = useState<string | undefined>(undefined)
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  // 加载知识库列表
  useEffect(() => {
    kbApi.list().then(setKbs).catch(() => {})
  }, [])

  // 自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const toggleKb = (id: string) => {
    setSelectedKbIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    // 切换知识库时重置会话，避免跨 KB 的混乱上下文
    setSessionId(undefined)
    setMessages([])
  }

  const handleSubmit = async () => {
    const q = question.trim()
    if (!q || selectedKbIds.size === 0) return

    setMessages((prev) => [...prev, { role: 'user', content: q }])
    setQuestion('')
    setLoading(true)
    setError('')

    try {
      const result = await qaApi.chat({
        question: q,
        kb_ids: Array.from(selectedKbIds),
        session_id: sessionId,
      })
      setSessionId(result.session_id)
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: result.answer, sources: result.sources },
      ])
    } catch (e) {
      setError(e instanceof Error ? e.message : '请求失败')
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: '抱歉，回答生成失败。' },
      ])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)]">
      {/* KB 选择 */}
      <div className="mb-4">
        <label className="block text-sm font-medium text-gray-700 mb-2">选择知识库</label>
        <div className="flex flex-wrap gap-2">
          {kbs.map((kb) => (
            <button
              key={kb.id}
              onClick={() => toggleKb(kb.id)}
              className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                selectedKbIds.has(kb.id)
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {kb.name}
            </button>
          ))}
          {kbs.length === 0 && (
            <p className="text-sm text-gray-400">暂无知识库</p>
          )}
        </div>
      </div>

      {/* 聊天区域 */}
      <div className="flex-1 overflow-y-auto space-y-4 mb-4 px-1">
        {messages.length === 0 && !error && (
          <div className="text-center text-gray-400 mt-20">
            <p className="text-lg mb-2">企业制度知识问答</p>
            <p className="text-sm">选择知识库，输入问题开始提问</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[80%] rounded-lg px-4 py-3 ${
                msg.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : 'bg-white border border-gray-200 text-gray-800'
              }`}
            >
              <p className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</p>

              {/* 来源引用 */}
              {msg.sources && msg.sources.length > 0 && (
                <details className="mt-2">
                  <summary className="text-xs cursor-pointer text-gray-500 hover:text-gray-700">
                    参考来源 ({msg.sources.length})
                  </summary>
                  <div className="mt-2 space-y-2">
                    {msg.sources.map((s, j) => (
                      <div key={j} className="text-xs bg-gray-50 rounded p-2">
                        <p className="font-medium text-gray-700 truncate">
                          {s.doc_source || '未知来源'}
                        </p>
                        <p className="text-gray-500 mt-1 line-clamp-2">{s.content_snippet}</p>
                        <p className="text-gray-400 mt-0.5">相关度: {s.relevance.toFixed(2)}</p>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          </div>
        ))}

        {/* 加载状态 */}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-white border border-gray-200 rounded-lg px-4 py-3">
              <div className="flex space-x-2">
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" />
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0.1s]" />
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0.2s]" />
              </div>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600">
          {error}
        </div>
      )}

      {/* 输入区域 */}
      <div className="flex gap-2 border-t pt-4">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleSubmit()
            }
          }}
          placeholder={selectedKbIds.size === 0 ? '请先选择知识库' : '输入问题...'}
          disabled={loading || selectedKbIds.size === 0}
          className="flex-1 px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-gray-100 disabled:cursor-not-allowed"
        />
        <button
          onClick={handleSubmit}
          disabled={loading || !question.trim() || selectedKbIds.size === 0}
          className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
        >
          发送
        </button>
      </div>
    </div>
  )
}
