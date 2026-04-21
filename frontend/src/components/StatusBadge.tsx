interface StatusBadgeProps {
  status: string
}

const statusColors: Record<string, string> = {
  // 索引状态
  none: 'bg-gray-100 text-gray-700',
  building: 'bg-yellow-100 text-yellow-700',
  ready: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  // 文档状态
  uploaded: 'bg-blue-100 text-blue-700',
  parsed: 'bg-blue-100 text-blue-700',
  indexed: 'bg-indigo-100 text-indigo-700',
  audit_pending: 'bg-purple-100 text-purple-700',
  auditing: 'bg-orange-100 text-orange-700',
  completed: 'bg-green-100 text-green-700',
  // 任务状态
  pending: 'bg-gray-100 text-gray-700',
  processing: 'bg-blue-100 text-blue-700',
  cancelled: 'bg-red-100 text-red-700',
  // 严重程度
  high: 'bg-red-100 text-red-700',
  medium: 'bg-yellow-100 text-yellow-700',
  low: 'bg-green-100 text-green-700',
}

const statusLabels: Record<string, string> = {
  none: '未索引',
  building: '构建中',
  ready: '就绪',
  failed: '失败',
  uploaded: '已上传',
  parsed: '已解析',
  indexed: '已索引',
  audit_pending: '待审核',
  auditing: '审核中',
  completed: '已完成',
  pending: '待处理',
  processing: '处理中',
  cancelled: '已取消',
  high: '高',
  medium: '中',
  low: '低',
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const colorClass = statusColors[status] || 'bg-gray-100 text-gray-700'
  const label = statusLabels[status] || status

  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colorClass}`}>
      {label}
    </span>
  )
}

interface ProgressBarProps {
  progress: number
  showLabel?: boolean
}

export function ProgressBar({ progress, showLabel = true }: ProgressBarProps) {
  const percentage = Math.round(progress * 100)

  return (
    <div className="flex items-center space-x-2">
      <div className="flex-1 bg-gray-200 rounded-full h-2">
        <div
          className="bg-blue-600 h-2 rounded-full transition-all duration-300"
          style={{ width: `${percentage}%` }}
        />
      </div>
      {showLabel && <span className="text-sm text-gray-600">{percentage}%</span>}
    </div>
  )
}

interface EmptyStateProps {
  title: string
  description?: string
  action?: React.ReactNode
}

export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="text-center py-12">
      <svg className="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
      </svg>
      <h3 className="mt-2 text-sm font-semibold text-gray-900">{title}</h3>
      {description && <p className="mt-1 text-sm text-gray-500">{description}</p>}
      {action && <div className="mt-6">{action}</div>}
    </div>
  )
}

interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg'
}

export function LoadingSpinner({ size = 'md' }: LoadingSpinnerProps) {
  const sizeClass = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  }[size]

  return (
    <div className="flex justify-center items-center py-8">
      <div className={`${sizeClass} animate-spin rounded-full border-2 border-gray-300 border-t-blue-600`} />
    </div>
  )
}
