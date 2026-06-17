import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'

const components: Components = {
  p: ({ children }) => (
    <p className="whitespace-pre-wrap leading-relaxed mb-2 last:mb-0">{children}</p>
  ),
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  ul: ({ children }) => (
    <ul className="list-disc list-inside mb-2 space-y-1">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal list-inside mb-2 space-y-1">{children}</ol>
  ),
  li: ({ children }) => <li>{children}</li>,
  code: ({ children }) => (
    <code className="bg-slate-200/70 rounded px-1 py-0.5 text-xs font-mono">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="bg-slate-200/50 rounded-lg p-3 overflow-x-auto text-sm mb-2">
      {children}
    </pre>
  ),
  h1: ({ children }) => (
    <h1 className="text-lg font-bold mb-2 mt-3">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-base font-bold mb-1 mt-2">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-sm font-semibold mb-1 mt-2">{children}</h3>
  ),
  a: ({ href, children }) => (
    <a href={href} className="text-blue-600 underline" target="_blank" rel="noreferrer">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-slate-300 pl-3 italic text-slate-600 mb-2">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto mb-2">
      <table className="min-w-full text-xs border-collapse border border-slate-200">
        {children}
      </table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-slate-200 bg-slate-100 px-2 py-1 font-medium text-left">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-slate-200 px-2 py-1">{children}</td>
  ),
}

export function Markdown({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {content}
    </ReactMarkdown>
  )
}
