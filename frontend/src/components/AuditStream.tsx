import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Brain, Wrench, FileSearch, AlertTriangle, CheckCircle2, XCircle, ChevronDown, ChevronRight } from 'lucide-react'
import type { AuditEvent, AuditEventIssue } from '../api/types'
import { Badge } from './Badge'

interface Props {
  taskId: string
  docId: string
}

const toolIcons: Record<string, React.ReactNode> = {
  get_structure: <FileSearch className="w-3.5 h-3.5 text-blue-500" />,
  read_chapter: <FileSearch className="w-3.5 h-3.5 text-indigo-500" />,
  search_kb: <FileSearch className="w-3.5 h-3.5 text-emerald-500" />,
  search_kb_text: <FileSearch className="w-3.5 h-3.5 text-teal-500" />,
  flag_issue: <AlertTriangle className="w-3.5 h-3.5 text-red-500" />,
}

const toolLabels: Record<string, string> = {
  get_structure: '查看文档结构',
  read_chapter: '读取章节',
  search_kb: '搜索知识库',
  search_kb_text: '文本搜索知识库',
  flag_issue: '记录问题',
}

const severityColors: Record<string, string> = {
  high: 'bg-red-100 text-red-700 border-red-200',
  medium: 'bg-amber-100 text-amber-700 border-amber-200',
  low: 'bg-slate-100 text-slate-600 border-slate-200',
}

function formatToolArgs(tool: string, args: Record<string, unknown>): string {
  if (tool === 'read_chapter') return `第 ${args.chapter_index} 章`
  if (tool === 'search_kb') return `"${args.query}"`
  if (tool === 'flag_issue') return ''
  return ''
}

function IssueCard({ issue }: { issue: AuditEventIssue }) {
  const pdfUrl = issue.standard_doc_id
    ? `/pdf-viewer/${issue.standard_doc_id}?page=${issue.standard_page_number ?? ''}&clause=${encodeURIComponent(issue.standard_clause || '')}&highlight=${encodeURIComponent(issue.standard_chunk_text || '')}`
    : null

  return (
    <div className={`mt-1 px-3 py-2 rounded-md border text-sm ${severityColors[issue.severity] || severityColors.medium}`}>
      <div className="flex items-center gap-2">
        <AlertTriangle className="w-3.5 h-3.5" />
        <span className="font-medium">
          #{issue.id} [{issue.severity === 'high' ? '高' : issue.severity === 'medium' ? '中' : '低'}风险]
        </span>
        <Badge value={issue.type} />
      </div>
      <p className="mt-1 leading-relaxed">{issue.description}</p>
      {(issue.standard_name || issue.standard_clause) && (
        <p className="mt-0.5 text-xs opacity-70">
          依据:{' '}
          {pdfUrl ? (
            <a href={pdfUrl} target="_blank" rel="noopener noreferrer"
              className="text-blue-500 hover:underline cursor-pointer">
              📄 {issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}
            </a>
          ) : (
            <span>{issue.standard_name}{issue.standard_clause ? ` § ${issue.standard_clause}` : ''}</span>
          )}
        </p>
      )}
    </div>
  )
}

export function AuditStream({ taskId, docId }: Props) {
  const navigate = useNavigate()
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [connected, setConnected] = useState(false)
  const [issuesCount, setIssuesCount] = useState(0)
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const containerRef = useRef<HTMLDivElement>(null)

  const retryCount = useRef(0)
  const hasErrored = useRef(false)
  const MAX_RETRIES = 5

  useEffect(() => {
    const baseUrl = import.meta.env.VITE_API_BASE_URL || ''
    const url = `${baseUrl}/api/v1/audit-tasks/${taskId}/stream`
    const es = new EventSource(url)

    es.onopen = () => {
      setConnected(true)
      hasErrored.current = false
      retryCount.current = 0
    }

    es.onmessage = (e) => {
      try {
        const event: AuditEvent = JSON.parse(e.data)
        setEvents((prev) => [...prev, event])
        if (event.type === 'issue_found') {
          setIssuesCount((c) => c + 1)
        }
      } catch {
        // ignore parse errors
      }
    }

    es.onerror = () => {
      setConnected(false)
      hasErrored.current = true
      retryCount.current += 1
      if (retryCount.current >= MAX_RETRIES) {
        es.close()
      }
      // 不调用 close()，让浏览器自动重连（EventSource 内置重连机制）
    }

    return () => es.close()
  }, [taskId])

  // Auto-scroll to bottom on new events
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [events])

  const toggleCollapse = (index: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  const lastEvent = events[events.length - 1]
  const isComplete = lastEvent?.type === 'complete'

  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 bg-slate-50 border-b border-slate-200">
        <div className="flex items-center gap-2">
          <Brain className="w-4 h-4 text-indigo-500" />
          <span className="text-sm font-medium text-slate-700">Agentic 审核进度</span>
          {!isComplete && (
            <span className="flex gap-1 ml-2">
              <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
              <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" style={{ animationDelay: '0.2s' }} />
              <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" style={{ animationDelay: '0.4s' }} />
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          {!connected && !isComplete && (
            <span className="flex items-center gap-1 text-amber-600">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400" /> 连接中…
            </span>
          )}
          {issuesCount > 0 && (
            <span className="flex items-center gap-1">
              <AlertTriangle className="w-3 h-3 text-red-400" />
              <span className="font-medium text-red-600">{issuesCount} 个问题</span>
            </span>
          )}
        </div>
      </div>

      {/* Event log */}
      <div ref={containerRef} className="max-h-96 overflow-y-auto bg-white">
        {events.length === 0 && !isComplete ? (
          <div className="flex items-center justify-center py-12 text-sm text-slate-400">
            等待 Agent 响应…
          </div>
        ) : (
          <div className="divide-y divide-slate-50">
            {events.map((event, i) => (
              <EventRow
                key={i}
                event={event}
                collapsed={collapsed.has(i)}
                onToggle={() => toggleCollapse(i)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Footer */}
      {isComplete && (
        <div className="px-4 py-3 bg-emerald-50 border-t border-emerald-200 flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm text-emerald-700">
            <CheckCircle2 className="w-4 h-4" />
            审核完成，共发现 {issuesCount} 个问题
          </div>
          <button
            className="px-3 py-1.5 text-xs font-medium bg-emerald-600 text-white rounded-md hover:bg-emerald-700 transition-colors"
            onClick={() => navigate(`/audit/${docId}/result/${taskId}`)}
          >
            查看结果
          </button>
        </div>
      )}
      {(lastEvent?.type === 'error' || (hasErrored.current && !connected && !isComplete && events.length === 0)) && (
        <div className="px-4 py-3 bg-red-50 border-t border-red-200 flex items-center gap-2 text-sm text-red-700">
          <XCircle className="w-4 h-4" />
          {lastEvent?.type === 'error' ? (lastEvent as { message: string }).message : '连接失败'}
        </div>
      )}
    </div>
  )
}

function EventRow({
  event,
  collapsed,
  onToggle,
}: {
  event: AuditEvent
  collapsed: boolean
  onToggle: () => void
}) {
  switch (event.type) {
    case 'start':
      return (
        <div className="px-4 py-2 text-xs text-slate-400 italic">
          🚀 {event.message}
        </div>
      )

    case 'reasoning':
      return (
        <div className="px-4 py-2">
          <div className="flex items-start gap-2">
            <Brain className="w-3.5 h-3.5 text-purple-400 mt-0.5 shrink-0" />
            <p className="text-xs text-slate-600 leading-relaxed whitespace-pre-wrap">
              {event.content.length > 500
                ? event.content.slice(0, 500) + '…'
                : event.content}
            </p>
          </div>
        </div>
      )

    case 'tool_call':
      return (
        <div className="px-4 py-2">
          <div className="flex items-center gap-2 text-xs">
            {toolIcons[event.tool] || <Wrench className="w-3.5 h-3.5 text-slate-400" />}
            <span className="font-medium text-slate-700">
              {toolLabels[event.tool] || event.tool}
            </span>
            <span className="text-slate-400">
              {formatToolArgs(event.tool, event.args)}
            </span>
          </div>
        </div>
      )

    case 'tool_result':
      return (
        <div className="px-4 py-2">
          <button
            className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700 w-full text-left"
            onClick={onToggle}
          >
            {collapsed ? (
              <ChevronRight className="w-3 h-3" />
            ) : (
              <ChevronDown className="w-3 h-3" />
            )}
            <span>
              返回 ({event.tool})
              {event.truncated ? ' [已截断]' : ''}
            </span>
          </button>
          {!collapsed && (
            <pre className="mt-1.5 ml-5 text-xs text-slate-500 bg-slate-50 rounded p-2 max-h-40 overflow-y-auto whitespace-pre-wrap">
              {event.content}
            </pre>
          )}
        </div>
      )

    case 'issue_found':
      return (
        <div className="px-4 py-2">
          <IssueCard issue={event.issue} />
        </div>
      )

    case 'progress':
      return (
        <div className="px-4 py-2 text-xs text-slate-500">{event.message}</div>
      )

    case 'cancelled':
      return (
        <div className="px-4 py-2 flex items-center gap-2 text-xs text-amber-600">
          <XCircle className="w-3.5 h-3.5" />
          {event.message}
        </div>
      )

    case 'error':
      return (
        <div className="px-4 py-2 flex items-center gap-2 text-xs text-red-600">
          <XCircle className="w-3.5 h-3.5" />
          {event.message}
        </div>
      )

    default:
      return null
  }
}
