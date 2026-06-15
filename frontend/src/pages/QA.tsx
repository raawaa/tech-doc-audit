import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Send, Loader2, MessageSquare, ChevronDown, ChevronRight } from 'lucide-react'
import { kbApi, qaApi } from '../api/endpoints'
import { Card, CardBody } from '../components/Card'

import type { QASource } from '../api/types'

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: QASource[]
}

export function QA() {
  const [selectedKBs, setSelectedKBs] = useState<string[]>([])
  const [question, setQuestion] = useState('')
  const [sessionId, setSessionId] = useState<string | undefined>()
  const [messages, setMessages] = useState<Message[]>([])
  const [expandedSources, setExpandedSources] = useState<Set<number>>(new Set())
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data: kbs = [] } = useQuery({
    queryKey: ['kbs'],
    queryFn: () => kbApi.list(),
  })

  const chat = useMutation({
    mutationFn: () => qaApi.chat({ question, kb_ids: selectedKBs, session_id: sessionId }),
    onSuccess: (data) => {
      setSessionId(data.session_id)
      setMessages((prev) => [...prev, { role: 'assistant', content: data.answer, sources: data.sources }])
    },
  })

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const handleSend = () => {
    if (!question.trim() || selectedKBs.length === 0) return
    setMessages((prev) => [...prev, { role: 'user', content: question }])
    chat.mutate()
    setQuestion('')
  }

  const toggleKB = (id: string) => {
    setSelectedKBs((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
    setSessionId(undefined)
    setMessages([])
  }

  return (
    <div className="space-y-6 h-[calc(100vh-4rem)] flex flex-col">
      <div>
        <h1 className="text-xl font-bold text-slate-900">知识问答</h1>
        <p className="mt-1 text-sm text-slate-500">基于知识库内容进行问答</p>
      </div>

      {/* KB selector */}
      <div className="flex flex-wrap gap-2">
        {kbs.map((kb) => (
          <button
            key={kb.id}
            onClick={() => toggleKB(kb.id)}
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
              selectedKBs.includes(kb.id) ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
            }`}
          >
            {kb.name}
          </button>
        ))}
        {kbs.length === 0 && <span className="text-xs text-slate-400">暂无知识库</span>}
      </div>

      {/* Chat area */}
      <Card className="flex-1 flex flex-col overflow-hidden">
        <CardBody className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-400">
              <MessageSquare className="w-10 h-10 mb-3" />
              <p className="text-sm">选择知识库后输入问题</p>
            </div>
          ) : (
            messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`max-w-[70%] rounded-lg px-4 py-3 text-sm ${
                  msg.role === 'user' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-900'
                }`}>
                  <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
                  {msg.sources && msg.sources.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-slate-200/60">
                      <button
                        className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700"
                        onClick={() => setExpandedSources((prev) => {
                          const next = new Set(prev)
                          next.has(i) ? next.delete(i) : next.add(i)
                          return next
                        })}
                      >
                        {expandedSources.has(i) ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                        {msg.sources.length} 个来源
                      </button>
                      {expandedSources.has(i) && (
                        <div className="mt-2 space-y-2">
                          {msg.sources.map((s, j) => (
                            <div key={j} className="text-xs text-slate-500 bg-white/80 rounded p-2 border border-slate-200/60">
                              <div className="flex items-center gap-2 mb-1">
                                <span className="font-medium text-slate-600">{s.doc_source}</span>
                                <span className="text-slate-400">{(s.relevance * 100).toFixed(0)}%</span>
                              </div>
                              <p className="line-clamp-2">{s.content_snippet}</p>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          {chat.isPending && (
            <div className="flex justify-start">
              <div className="bg-slate-100 rounded-lg px-4 py-3">
                <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </CardBody>

        {/* Input */}
        <div className="border-t border-slate-200 p-4">
          <div className="flex gap-2">
            <input
              className="input flex-1"
              placeholder={selectedKBs.length === 0 ? '请先选择知识库' : '输入问题…'}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
              disabled={selectedKBs.length === 0 || chat.isPending}
            />
            <button className="btn-primary" onClick={handleSend} disabled={!question.trim() || selectedKBs.length === 0 || chat.isPending}>
              {chat.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            </button>
          </div>
        </div>
      </Card>
    </div>
  )
}
