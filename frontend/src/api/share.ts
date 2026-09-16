// 会话共享 API：与 client.ts 同范式（原生 fetch + credentials:'include' + CSRF 双提交）。
// 公开查看 GET /share/{token} 也走这里，但它不依赖登录态。
import { apiFetch, API_BASE } from './client'

export interface ShareResult {
  share_token: string
  share_url: string
}

/** 把本人会话设为公开只读，返回令牌与可分享链接。需鉴权（本人会话）。 */
export async function shareSession(sessionId: string): Promise<ShareResult> {
  return apiFetch<ShareResult>(`/sessions/${sessionId}/share`, { method: 'POST' })
}

/** 取消共享（删除令牌），原链接立即失效。需鉴权（本人会话）。 */
export async function unshareSession(sessionId: string): Promise<void> {
  await apiFetch(`/sessions/${sessionId}/share`, { method: 'DELETE' })
}

/** 公开只读查看：凭令牌取回会话，无需登录（GET 天然豁免 CSRF）。 */
export async function fetchSharedSession(token: string): Promise<unknown> {
  const res = await fetch(`${API_BASE}/share/${encodeURIComponent(token)}`, {
    method: 'GET',
    credentials: 'include',
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail)
    } catch {
      /* keep */
    }
    throw new Error(detail || `加载失败（${res.status}）`)
  }
  return res.json()
}
