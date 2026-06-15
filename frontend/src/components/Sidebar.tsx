import { NavLink } from 'react-router-dom'
import { FileText, BookOpen, MessageSquare } from 'lucide-react'

const links = [
  { to: '/audit', label: '文档审核', icon: FileText },
  { to: '/knowledge-bases', label: '知识库', icon: BookOpen },
  { to: '/qa', label: '知识问答', icon: MessageSquare },
]

export function Sidebar() {
  return (
    <aside className="w-56 bg-[#1e3a5f] text-white flex flex-col shrink-0">
      <div className="h-14 flex items-center px-5 border-b border-white/10">
        <FileText className="w-5 h-5 mr-2.5 text-blue-300" />
        <span className="font-semibold text-sm tracking-wide">审核系统</span>
      </div>
      <nav className="flex-1 py-4 px-3 space-y-1">
        {links.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/audit'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-white/15 text-white'
                  : 'text-blue-200/80 hover:text-white hover:bg-white/8'
              }`
            }
          >
            <Icon className="w-4 h-4 shrink-0" />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="px-5 py-3 border-t border-white/10">
        <p className="text-xs text-blue-300/60">技术文档审核系统 v0.2</p>
      </div>
    </aside>
  )
}
