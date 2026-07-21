import { Fragment, useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useChat } from '@ai-sdk/react'
import { DefaultChatTransport } from 'ai'
import { Send, Loader2, MessageSquare, Copy, RefreshCw, Square, FileText } from 'lucide-react'
import { kbApi } from '../api/endpoints'
import { Card, CardBody } from '../components/Card'
import { Markdown } from '../components/Markdown'

import type { QASource } from '../api/types'
import {
  buildQASourcePreviewUrl,
  extractQASourceFromPart,
} from '../lib/qaSource'

/** 从 AI SDK v6 的 UIMessage 中提取文本内容 */
function getMessageText(msg: { parts?: Array<{ type: string; text?: string }>; content?: string }): string {
  // v6 用 parts: [{ type: 'text', text: '...' }]
  let text = ''
  if (msg.parts && msg.parts.length > 0) {
    text = msg.parts.filter(p => p.type === 'text').map(p => (p as { text?: string }).text || '').join('')
  } else {
    text = (msg as { content?: string }).content || ''
  }
  // 过滤掉追问建议行（以【追问】开头），它们由专用区块渲染为 chip 按钮
  return text.split('\n').filter(line => !line.trim().startsWith('【追问】')).join('\n')
}

// ── V9 PRD #67 — 内联 source-document chip & 进度指示器 ────────────────────────
//
// 设计要点（见 CONTEXT.md "QA 引用体验（V9 PRD #67）"）：
// - 工具调用 `input-*` 状态渲染 <AgentStepIndicator>（同位置占位，spinner）
// - 工具调用 `output-available` + 紧随 source-document：进度条就地升级为
//   <SourceChip>，两者复用同一个 React key（toolCallId），DOM 节点不重建。
// - 工具调用 `output-available` 但无 source-document（search_kb_text / 无 doc_id
//   / 空结果）：进度条**直接消失**，不留下孤儿元素。
// - source-document part 本身不渲染（其 chip 由对应 tool-* part 触发渲染）。
// - 无工具调用的回答自然无 chip / 进度条。

/** 计算某消息内 source-document part 与 toolCallId 的归属关系（流顺序）。 */
function indexSourceDocsByToolCall(
  parts: ReadonlyArray<{ type: string; toolCallId?: string }> | undefined,
): Record<string, QASource[]> {
  const map: Record<string, QASource[]> = {}
  if (!parts) return map
  let currentCallId: string | null = null
  for (const part of parts) {
    const type = part.type
    // AI SDK 工具 part 跨状态共享同一个 toolCallId；
    // toolCallId 一旦出现（含 input-streaming / input-available / output-available）
    // 就锁定为后续 source-document 的归属。
    if (type.startsWith('tool-') && (part as { toolCallId?: string }).toolCallId) {
      currentCallId = (part as { toolCallId?: string }).toolCallId || null
    }
    if (type === 'source-document') {
      const qa = extractQASourceFromPart(part)
      if (currentCallId && qa) {
        if (!map[currentCallId]) map[currentCallId] = []
        // 同 doc_id 在同一 tool call 内已由后端 _emit_source_documents 去重；
        // 前端再保险一道，避免 React key 重复。
        if (!map[currentCallId].some((s) => s.doc_id === qa.doc_id)) {
          map[currentCallId].push(qa)
        }
      }
    }
  }
  return map
}

function AgentStepIndicator({ name }: { name?: string }) {
  // 与 SourceChip 同高度、同内边距，确保"就地升级"无视觉跳变
  return (
    <span className="qa-chip qa-chip--loading inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs border border-slate-200 bg-white/70 text-slate-500 align-middle">
      <Loader2 className="w-3.5 h-3.5 animate-spin" />
      <span>🧠 {name ? `${name} 搜索中…` : '搜索中…'}</span>
    </span>
  )
}

function SourceChip({ source }: { source: QASource }) {
  const url = buildQASourcePreviewUrl(source)
  const tooltip = (source.content_snippet || '').trim()
  const label = (source.doc_source || '未知来源').trim() || '未知来源'

  if (!url) {
    // doc_id 缺失（理论上 buildQASourcePayload 已过滤，这里再兜底）
    return (
      <span
        className="qa-chip qa-chip--disabled inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs border border-slate-200 bg-slate-100 text-slate-400 align-middle cursor-not-allowed"
        title={tooltip || '该来源无文档 ID，无法预览'}
      >
        <FileText className="w-3.5 h-3.5" />
        <span className="truncate max-w-[12rem]">{label}</span>
      </span>
    )
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={tooltip || label}
      className="qa-chip qa-chip--source inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs border border-slate-200 bg-blue-50 text-blue-700 hover:bg-blue-100 hover:border-blue-300 transition-colors align-middle"
    >
      <FileText className="w-3.5 h-3.5 shrink-0" />
      <span className="truncate max-w-[12rem]">{label}</span>
    </a>
  )
}

/** 渲染某消息内一个 tool-* part（按 state 切换 indicator ↔ chip）。 */
function renderToolPart(
  part: { type: string; toolCallId?: string; toolName?: string; state: string },
  sourceDocsByToolCall: Record<string, QASource[]>,
): React.ReactNode {
  const toolCallId = part.toolCallId
  const name = part.toolName || part.type.replace(/^tool-/, '')
  const isLoading = part.state === 'input-streaming' || part.state === 'input-available'

  if (isLoading) {
    // 进度条 → chip 升级路径：React key 锁定为 toolCallId。
    return <AgentStepIndicator key={toolCallId || name} name={name} />
  }

  // state === 'output-available'（AI SDK 已知 tool-* 的终态仅此一种）。
  const sources = toolCallId ? sourceDocsByToolCall[toolCallId] : undefined
  if (!sources || sources.length === 0) {
    // search_kb_text / 空结果 / 无 doc_id：进度条直接消失，不渲染任何元素。
    return null
  }

  // 升级：第一个 chip 复用 toolCallId 作为 React key，DOM 锚不重建；
  // 同一 tool call 产生的后续 chip 用 sourceId 区分。
  const first = sources[0]
  return (
    <Fragment key={toolCallId || first.doc_id}>
      <SourceChip source={first} />
      {sources.slice(1).map((s) => (
        <SourceChip key={s.doc_id + '_' + (s.page_number ?? 0)} source={s} />
      ))}
    </Fragment>
  )
}

export function QA() {
  const [selectedKBs, setSelectedKBs] = useState<string[]>([])
  const [sessionId, setSessionId] = useState<string | undefined>()
  const [suggestionsMap, setSuggestionsMap] = useState<Map<string, string[]>>(new Map())
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
    onFinish: ({ message }) => {
      // PRD #67: data-sources 已下线；data-session / data-suggestions 仍保留
      // （session_id 维护追问；suggestions 渲染为底部追问 chip 按钮）。
      const dataParts = message.parts?.filter((p) => p.type.startsWith('data-')) as
        | Array<{ data: { session_id?: string; suggestions?: string[] } }>
        | undefined
      const sess = dataParts?.find((p) => p.data?.session_id)
      if (sess?.data?.session_id) {
        setSessionId(sess.data.session_id)
      }
      const sug = dataParts?.find((p) => p.data?.suggestions)
      if (sug?.data?.suggestions) {
        setSuggestionsMap((prev) => new Map(prev).set(message.id, sug.data!.suggestions!))
      }
    },
  })

  const isStreaming = chat.status === 'streaming'

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [chat.messages, isStreaming])

  const handleSend = useCallback(() => {
    const q = input.trim()
    if (!q || selectedKBs.length === 0 || isStreaming) return
    setInput('')
    chat.sendMessage({ text: q })
  }, [input, selectedKBs, isStreaming, chat.sendMessage])

  const handleSendWithText = useCallback((text: string) => {
    if (selectedKBs.length === 0 || isStreaming) return
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
    setSuggestionsMap(new Map())
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
                const sourceDocsByToolCall = indexSourceDocsByToolCall(
                  msg.parts as ReadonlyArray<{ type: string; toolCallId?: string }> | undefined,
                )
                return (
                  <div key={msg.id || i} className="space-y-1.5">
                    <div className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[70%] rounded-lg px-4 py-3 text-sm ${
                        msg.role === 'user' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-900'
                      }`}>
                        {msg.parts && msg.parts.length > 0 ? (
                          // 内联 flex 布局：文本与 chip 按流顺序交错；
                          // `flex-wrap` 保证 chip 与文本自然衔接（不强制每段独占一行）。
                          <div className="markdown-body inline-flex flex-wrap items-baseline gap-x-1.5 gap-y-1.5">
                            {msg.parts.map((part, pi) => {
                              const type = (part as { type: string }).type
                              if (type === 'text') {
                                const text = ((part as { text?: string }).text || '')
                                  .split('\n')
                                  .filter((line) => !line.trim().startsWith('【追问】'))
                                  .join('\n')
                                if (!text) return null
                                return (
                                  <span key={`t-${pi}`} className="inline">
                                    <Markdown content={text} />
                                  </span>
                                )
                              }
                              if (type === 'reasoning') {
                                const reasoningText = (part as { text?: string }).text || ''
                                return (
                                  <details key={`r-${pi}`} className="mb-2 w-full">
                                    <summary className="text-xs text-slate-400 cursor-pointer">💭 推理过程</summary>
                                    <div className="mt-1 text-xs text-slate-500 whitespace-pre-wrap">{reasoningText}</div>
                                  </details>
                                )
                              }
                              if (type.startsWith('tool-')) {
                                return (
                                  <span key={`tp-${(part as { toolCallId?: string }).toolCallId || pi}`} className="inline-flex">
                                    {renderToolPart(
                                      part as { type: string; toolCallId?: string; toolName?: string; state: string },
                                      sourceDocsByToolCall,
                                    )}
                                  </span>
                                )
                              }
                              // source-document 由 renderToolPart 触发渲染（tool-* 同位置升级），
                              // 此处直接吞掉，避免重复 chip。
                              if (type === 'source-document') return null
                              return null
                            })}
                          </div>
                        ) : (
                          <div className="markdown-body"><Markdown content={getMessageText(msg)} /></div>
                        )}
                        {/* 流式传输中，文本尚未生成时显示加载指示器 */}
                        {isStreaming && i === chat.messages.length - 1 && msg.role === 'assistant' && !getMessageText(msg) && (
                          <div className="flex items-center gap-2 mt-2">
                            <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
                            <span className="text-xs text-slate-400">正在生成回答…</span>
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