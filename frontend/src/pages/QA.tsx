import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useChat } from '@ai-sdk/react'
import { DefaultChatTransport } from 'ai'
import { Send, Loader2, MessageSquare, ChevronDown, ChevronRight, Copy, RefreshCw, Square } from 'lucide-react'
import { kbApi } from '../api/endpoints'
import { Card, CardBody } from '../components/Card'

import type { QASource } from '../api/types'

/** 从 AI SDK v6 的 UIMessage 中提取文本内容 */
function getMessageText(msg: { parts?: Array<{ type: string; text?: string }>; content?: string }): string {
  // v6 用 parts: [{ type: 'text', text: '...' }]
  let text = ''
  if (msg.parts && msg.parts.length > 0) {
    text = msg.parts.filter(p => p.type === 'text').map(p => p.text).join('')
  } else {
    text = msg.content || ''
  }
  // 过滤掉追问建议行（以【追问】开头），它们由专用区块渲染为 chip 按钮
  return text.split('\n').filter(line => !line.trim().startsWith('【追问】')).join('\n')
}

export function QA() {
  const [selectedKBs, setSelectedKBs] = useState<string[]>([])
  const [sessionId, setSessionId] = useState<string | undefined>()
  const [sourcesMap, setSourcesMap] = useState<Map<string, QASource[]>>(new Map())
  const [suggestionsMap, setSuggestionsMap] = useState<Map<string, string[]>>(new Map())
  const [expandedSources, setExpandedSources] = useState<Set<string>>(new Set())
  const [progressLabel, setProgressLabel] = useState('')
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  // 用 ref 跟踪最新值，避免 transport 函数闭包陈腐
  const sessionIdRef = useRef(sessionId)
  sessionIdRef.current = sessionId
  const selectedKBsRef = useRef(selectedKBs)
  selectedKBsRef.current = selectedKBs

  const { data: kbs = [] } = useQuery({
    queryKey: ['kbs'],
    queryFn: () => kbApi.list(),
  })

  const chat = useChat({
    transport: new DefaultChatTransport({
      api: '/api/v1/qa/chat/stream',
      // body 用函数确保每次请求使用最新值
      body: () => ({
        kb_ids: selectedKBsRef.current,
        session_id: sessionIdRef.current,
      }),
    }),
    experimental_throttle: 50,
    onData: (dataPart) => {
      // 实时消费服务端 data-progress 事件，更新进度标签
      const data = dataPart.data as { label?: string; sources?: QASource[] } | undefined
      if (dataPart.type === 'data-progress' && data?.label) {
        setProgressLabel(data.label)
      }
    },
    onFinish: ({ message }) => {
      // 从 message.parts 中提取自定义数据（data-* 事件，类型为 `data-${string}`）
      const dataParts = message.parts?.filter(p => p.type.startsWith('data-')) as Array<{ data: { sources?: QASource[]; session_id?: string; suggestions?: string[] } }> | undefined
      // data-sources: 查找 data 中有 sources 字段的
      const src = dataParts?.find(p => p.data?.sources)
      if (src?.data?.sources) {
        setSourcesMap(prev => new Map(prev).set(message.id, src.data.sources!))
      }
      // data-session: 查找 data 中有 session_id 字段的
      const sess = dataParts?.find(p => p.data?.session_id)
      if (sess?.data?.session_id) {
        setSessionId(sess.data.session_id)
      }
      // data-suggestions: 查找 data 中有 suggestions 字段的
      const sug = dataParts?.find(p => p.data?.suggestions)
      if (sug?.data?.suggestions) {
        setSuggestionsMap(prev => new Map(prev).set(message.id, sug.data!.suggestions!))
      }
    },
  })

  const isStreaming = chat.status === 'streaming'

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [chat.messages, isStreaming])

  const handleSend = useCallback(() => {
    const q = input.trim()
    if (!q || selectedKBs.length === 0 || isStreaming) return
    setProgressLabel('正在准备...')
    setInput('')
    chat.sendMessage({ text: q })
  }, [input, selectedKBs, isStreaming, chat.sendMessage])

  // 用于追问建议直接发送
  const handleSendWithText = useCallback((text: string) => {
    if (selectedKBs.length === 0 || isStreaming) return
    setProgressLabel('正在准备...')
    setInput('')
    chat.sendMessage({ text })
  }, [selectedKBs, isStreaming, chat.sendMessage])

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setInput(e.target.value)
  }, [])

  const toggleKB = (id: string) => {
    chat.stop()
    chat.clearError()
    chat.setMessages([])
    setSelectedKBs((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
    setSessionId(undefined)
    setSourcesMap(new Map())
    setSuggestionsMap(new Map())
    setProgressLabel('')
    setInput('')
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
          {chat.messages.length === 0 && !isStreaming ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-400">
              <MessageSquare className="w-10 h-10 mb-3" />
              <p className="text-sm">选择知识库后输入问题</p>
            </div>
          ) : (
            <>
              {chat.messages.map((msg, i) => {
                // 流式传输时，最后一条助手消息由下方的专用区块渲染，避免重复
                if (isStreaming && i === chat.messages.length - 1 && msg.role === 'assistant') return null
                return (
                  <div key={msg.id || i} className="space-y-1.5">
                    <div className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[70%] rounded-lg px-4 py-3 text-sm ${
                        msg.role === 'user' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-900'
                      }`}>
                        <p className="whitespace-pre-wrap leading-relaxed">{getMessageText(msg)}</p>
                        {msg.role === 'assistant' && sourcesMap.has(msg.id) && (
                        <div className="mt-2 pt-2 border-t border-slate-200/60">
                          <button
                            className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700"
                            onClick={() => setExpandedSources((prev) => {
                              const next = new Set(prev)
                              next.has(msg.id) ? next.delete(msg.id) : next.add(msg.id)
                              return next
                            })}
                          >
                            {expandedSources.has(msg.id) ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                            {sourcesMap.get(msg.id)!.length} 个来源
                          </button>
                          {expandedSources.has(msg.id) && (
                            <div className="mt-2 space-y-2">
                              {sourcesMap.get(msg.id)!.map((s, j) => (
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
                    {/* 助手消息操作按钮 */}
                    {msg.role === 'assistant' && (
                      <div className="flex justify-start gap-2 pl-2">
                        <button
                          className="text-xs text-slate-400 hover:text-slate-600 transition-colors"
                          onClick={() => navigator.clipboard.writeText(getMessageText(msg))}
                          title="复制回答"
                        >
                          <Copy className="w-3.5 h-3.5" />
                        </button>
                        {!isStreaming && i === chat.messages.length - 1 && (
                          <button
                            className="text-xs text-slate-400 hover:text-slate-600 transition-colors"
                            onClick={() => chat.regenerate()}
                            title="重新生成"
                          >
                            <RefreshCw className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                    )}
                    {/* 追问建议 */}
                    {msg.role === 'assistant' && suggestionsMap.has(msg.id) && (
                      <div className="flex justify-start gap-2 pl-2 flex-wrap">
                        {suggestionsMap.get(msg.id)!.map((suggestion, j) => (
                          <button
                            key={j}
                            className="text-xs bg-white border border-slate-200 text-slate-600 rounded-full px-3 py-1 hover:bg-slate-50 hover:border-slate-300 transition-colors"
                            onClick={() => {
                              setInput(suggestion)
                              handleSendWithText(suggestion)
                            }}
                          >
                            {suggestion}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
              )
              })}

              {/* 错误提示 */}
              {chat.error && (
                <div className="flex justify-start">
                  <div className="max-w-[70%] rounded-lg px-4 py-3 text-sm bg-red-50 text-red-700 border border-red-200 flex items-center gap-3">
                    <span className="flex-1">❌ {chat.error.message || '请求失败'}</span>
                    <button
                      className="text-xs font-medium text-red-600 hover:text-red-800 underline whitespace-nowrap"
                      onClick={() => chat.regenerate()}
                    >
                      重试
                    </button>
                  </div>
                </div>
              )}

              {/* 流式输出中的进度指示 */}
              {isStreaming && chat.messages[chat.messages.length - 1]?.role === 'assistant' && (
                <div className="flex justify-start">
                  <div className="max-w-[70%] rounded-lg px-4 py-3 text-sm bg-slate-100 text-slate-900">
                    <p className="whitespace-pre-wrap leading-relaxed">
                      {getMessageText(chat.messages[chat.messages.length - 1])}
                    </p>
                    {!getMessageText(chat.messages[chat.messages.length - 1]) && (
                      <div className="flex items-center gap-2">
                        <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
                        <span className="text-xs text-slate-400">{progressLabel}</span>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
          <div ref={bottomRef} />
        </CardBody>

        {/* Input */}
        <div className="border-t border-slate-200 p-4">
          <div className="flex gap-2">
            <input
              className="input flex-1"
              placeholder={selectedKBs.length === 0 ? '请先选择知识库' : '输入问题…'}
              value={input}
              onChange={handleInputChange}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
              disabled={selectedKBs.length === 0 || isStreaming}
            />
            {isStreaming ? (
              <button className="btn-secondary flex items-center gap-1.5" onClick={() => chat.stop()}>
                <Square className="w-4 h-4" /> 停止
              </button>
            ) : (
              <button className="btn-primary" onClick={handleSend} disabled={!input.trim() || selectedKBs.length === 0}>
                <Send className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </Card>
    </div>
  )
}
