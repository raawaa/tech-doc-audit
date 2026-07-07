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
  // KB index_status (per-doc embedding_status reuses these 终态色系)
  none: 'bg-slate-100 text-slate-500',
  building: 'bg-blue-50 text-blue-700',
  pending_index: 'bg-slate-100 text-slate-600',
  indexing: 'bg-indigo-50 text-indigo-700',
  // 终态：KB index_status = 'searchable'（per ADR-0003）
  searchable: 'bg-emerald-50 text-emerald-700',
  // 终态：doc embedding_status = 'embedded'（per ADR-0003）
  embedded: 'bg-emerald-50 text-emerald-700',
  // category
  national: 'bg-purple-50 text-purple-700',
  industry: 'bg-blue-50 text-blue-700',
  enterprise: 'bg-slate-100 text-slate-600',
  // audit type
  compliance: 'bg-red-50 text-red-700',
  completeness: 'bg-amber-50 text-amber-700',
  consistency: 'bg-blue-50 text-blue-700',
  insufficient_evidence: 'bg-slate-100 text-slate-500',
  out_of_scope: 'bg-slate-100 text-slate-400',
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
  none: '未建索引',
  building: '构建中',
  pending_index: '待索引',
  indexing: '索引中',
  searchable: '可检索',
  embedded: '已向量化',
  national: '国家标准',
  industry: '行业标准',
  enterprise: '企业标准',
  compliance: '合规性',
  completeness: '完整性',
  consistency: '一致性',
  insufficient_evidence: '证据不足',
  out_of_scope: '超出范围',
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
        // 'whitespace-nowrap shrink-0' 防止 Badge 文本在窄列里换行（issue #46）：
        // 中文标签"已向量化"在 w-24 列宽下会折行；shrink-0 防止 flex 容器挤压。
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap shrink-0',
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
