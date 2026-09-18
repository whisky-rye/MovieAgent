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

// O11 用户反馈闭环：赞踩/点击上报（POST，带 CSRF 双提交）
// signal 语义：like/dislike 为互斥的显式反馈；click 为隐式埋点；cancel 撤销该作品的赞/踩。
export type FeedbackSignal = 'like' | 'dislike' | 'click' | 'cancel'
export type FeedbackState = 'like' | 'dislike'

export interface FeedbackPayload {
  item_id: string
  signal: FeedbackSignal
  genres?: string[]
  rank?: number
  query?: string
  // 显式携带当前用户身份：消除"刷新后恢复不到"的环境歧义（不再仅靠 cookie 推断用户）。
  // AUTH 开启时后端以 token 内 user_id 为准、忽略此字段；AUTH 关闭时作为显式身份。
  user_id?: string
}

export async function postFeedback(payload: FeedbackPayload): Promise<{ ok: boolean; state?: FeedbackState | null }> {
  return apiFetch<{ ok: boolean; state?: FeedbackState | null }>('/feedback', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// 拉取当前用户已赞/已踩的作品，用于刷新或重进会话后恢复按钮选中态。
// userId 显式传入时拼到 query，确保恢复的是「同一用户」的历史（避免落到 default）。
export async function fetchMyFeedback(userId?: string): Promise<{ items: Record<string, FeedbackState> }> {
  const qs = userId ? `?user_id=${encodeURIComponent(userId)}` : ''
  return apiFetch<{ items: Record<string, FeedbackState> }>(`/feedback/mine${qs}`)
}
