import { useQuery } from '@tanstack/react-query'
import { WifiOff } from 'lucide-react'
import { api } from '../api/client'

export function HealthBanner() {
  const { isError } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.get('/health').then(r => r.data),
    refetchInterval: 30_000,
    retry: 1,
  })

  if (!isError) return null

  return (
    <div className="bg-red-600 text-white px-4 py-2 flex items-center justify-center gap-2 text-sm shrink-0">
      <WifiOff className="w-4 h-4" />
      <span>后端服务不可用，请检查服务是否正常运行</span>
    </div>
  )
}
