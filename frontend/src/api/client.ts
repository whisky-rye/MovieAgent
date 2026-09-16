// 最小化的 API 客户端：基于原生 fetch，无第三方 HTTP 库。
// - 统一拼接 VITE_API_BASE（默认 http://localhost:8000，与后端 CORS 白名单一致）
// - 鉴权改为 httpOnly Cookie（ma_token）：fetch 带 credentials:'include' 自动携带，
//   JS 读不到 token，防 XSS 窃取；不再使用 localStorage 存 token。
// - 防 CSRF：非安全方法(GET 除外)从可读 Cookie(ma_csrf)取值，放入 X-CSRF-Token 头做双提交校验。

export const API_BASE = (import.meta.env.VITE_API_BASE ?? 'http://localhost:8000').replace(/\/$/, '')

export interface ApiError extends Error {
  status: number
  detail: string
}

// 读取指定 Cookie（用于拿 ma_csrf 做 CSRF 双提交）
export function getCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
  return m ? decodeURIComponent(m[1]) : null
}

const CSRF_COOKIE = 'ma_csrf'
const CSRF_HEADER = 'X-CSRF-Token'

export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')

  // 非 GET 请求：附带 CSRF 双提交 token（从可读 Cookie 读取）
  const method = (init.method ?? 'GET').toUpperCase()
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = getCookie(CSRF_COOKIE)
    if (csrf) headers.set(CSRF_HEADER, csrf)
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    method: init.method ?? 'GET',
    headers,
    credentials: 'include', // 携带 httpOnly 鉴权 Cookie
  })

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

// G-Poster：轮询会话海报状态（GET，无需 CSRF）
export interface PosterStatus {
  status: 'none' | 'pending' | 'ready' | 'error'
  url?: string
  download_url?: string
  model?: string
  model_family?: 'image' | 'video'
  media_type?: string
  placeholder?: boolean
  error?: string
}

export async function fetchPoster(sessionId: string): Promise<PosterStatus> {
  return apiFetch<PosterStatus>(`/poster/${encodeURIComponent(sessionId)}`)
}
