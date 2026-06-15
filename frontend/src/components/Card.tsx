import type { ReactNode } from 'react'
import { twMerge } from 'tailwind-merge'

export function Card({
  children,
  className,
  onClick,
}: {
  children: ReactNode
  className?: string
  onClick?: () => void
}) {
  return (
    <div className={twMerge('card', className)} onClick={onClick}>
      {children}
    </div>
  )
}

export function CardHeader({
  title,
  action,
  className,
}: {
  title: string
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={twMerge('card-header flex items-center justify-between', className)}>
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      {action && <div className="flex items-center gap-2">{action}</div>}
    </div>
  )
}

export function CardBody({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <div className={twMerge('card-body', className)}>
      {children}
    </div>
  )
}
