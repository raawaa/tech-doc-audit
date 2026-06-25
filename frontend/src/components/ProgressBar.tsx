import { twMerge } from 'tailwind-merge'

export function ProgressBar({
  value,
  className,
  animated = true,
  indeterminate = false,
}: {
  value: number  // 0.0 ~ 1.0
  className?: string
  animated?: boolean
  indeterminate?: boolean
}) {
  return (
    <div className={twMerge('w-full h-2 bg-slate-100 rounded-full overflow-hidden', className ?? '')}>
      {indeterminate ? (
        <div className="h-full rounded-full progress-indeterminate" />
      ) : (
        <div
          className={`h-full bg-blue-600 rounded-full${animated ? ' transition-all duration-500' : ''}`}
          style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }}
        />
      )}
    </div>
  )
}
