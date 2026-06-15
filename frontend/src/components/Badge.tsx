import type { ReactNode } from 'react'
import { twMerge } from 'tailwind-merge'

const colorMap: Record<string, string> = {
  // status
  uploaded: 'bg-slate-100 text-slate-600',
  parsed: 'bg-blue-50 text-blue-700',
  indexed: 'bg-indigo-50 text-indigo-700',
  audit_pending: 'bg-amber-50 text-amber-700',
  auditing: 'bg-amber-100 text-amber-800',
  completed: 'bg-emerald-50 text-emerald-700',
  failed: 'bg-red-50 text-red-700',
  cancelled: 'bg-slate-100 text-slate-500',
  pending: 'bg-slate-100 text-slate-600',
  processing: 'bg-blue-50 text-blue-700',
  // severity
  high: 'bg-red-50 text-red-700',
  medium: 'bg-amber-50 text-amber-700',
  low: 'bg-emerald-50 text-emerald-700',
  // index status
  none: 'bg-slate-100 text-slate-500',
  building: 'bg-blue-50 text-blue-700',
  ready: 'bg-emerald-50 text-emerald-700',
  // category
  national: 'bg-purple-50 text-purple-700',
  industry: 'bg-blue-50 text-blue-700',
  enterprise: 'bg-slate-100 text-slate-600',
  // audit type
  compliance: 'bg-red-50 text-red-700',
  completeness: 'bg-amber-50 text-amber-700',
  consistency: 'bg-blue-50 text-blue-700',
}

const labelMap: Record<string, string> = {
  uploaded: '已上传',
  parsed: '已解析',
  indexed: '已索引',
  audit_pending: '待审核',
  auditing: '审核中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  pending: '待执行',
  processing: '进行中',
  high: '高',
  medium: '中',
  low: '低',
  none: '未建立',
  building: '构建中',
  ready: '就绪',
  national: '国家标准',
  industry: '行业标准',
  enterprise: '企业标准',
  compliance: '合规性',
  completeness: '完整性',
  consistency: '一致性',
}

export function Badge({
  value,
  className,
  children,
}: {
  value: string
  className?: string
  children?: ReactNode
}) {
  return (
    <span
      className={twMerge(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        colorMap[value] || 'bg-slate-100 text-slate-600',
        className,
      )}
    >
      {children || labelMap[value] || value}
    </span>
  )
}

export function SeverityDot({ severity }: { severity: string }) {
  const colors: Record<string, string> = {
    high: 'bg-red-500',
    medium: 'bg-amber-500',
    low: 'bg-emerald-500',
  }
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${colors[severity] || 'bg-slate-400'}`}
      title={labelMap[severity] || severity}
    />
  )
}
