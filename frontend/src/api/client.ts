// 最小化的 API 客户端：基于原生 fetch，无第三方 HTTP 库。
// - 统一拼接 VITE_API_BASE（默认 http://localhost:8000，与后端 CORS 白名单一致）
// - 自动附加 Bearer token（来自 localStorage）
// - 把非 2xx 响应归一化为带 status/detail 的错误

export const API_BASE = (import.meta.env.VITE_API_BASE ?? 'http://localhost:8000').replace(/\/$/, '')

export interface ApiError extends Error {
  status: number
  detail: string
}

export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem('ma_token')
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers })

  let body: unknown = null
  const text = await res.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }

  if (!res.ok) {
    const detail = (body && typeof body === 'object' && 'detail' in body
      ? (body as Record<string, unknown>).detail
      : null) as string | null || res.statusText
    const err = new Error(detail) as ApiError
    err.status = res.status
    err.detail = detail
    throw err
  }

  return body as T
}
