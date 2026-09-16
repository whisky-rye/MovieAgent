// 轻量鉴权 store：用 Vue 的 reactive + computed 替代 Pinia（满足"最少依赖"原则）。
// 单一实例；鉴权改为 httpOnly Cookie（后端下发，JS 读不到），本地不再存 token，
// 规避 XSS 窃取；CSRF 由 api/client 在请求头双提交。

import { reactive, computed } from 'vue'
import { apiFetch } from '../api/client'

export interface AuthUser {
  user_id: string
  username: string
}

interface SessionPayload {
  user_id: string
  username: string
  token: string
}

interface AuthState {
  user: AuthUser | null
  ready: boolean // 启动续期校验是否完成（避免首帧闪登录页）
}

const state = reactive<AuthState>({
  user: null,
  ready: false,
})

// 应用启动：带 Cookie 调 /auth/me，命中则视为已登录；否则保持未登录（Cookie 由后端管控）。
export async function initAuth(): Promise<void> {
  try {
    const me = await apiFetch<AuthUser>('/auth/me')
    state.user = me
  } catch {
    state.user = null
  } finally {
    state.ready = true
  }
}

export const authUser = computed(() => state.user)
export const isLoggedIn = computed(() => !!state.user)
export const authReady = computed(() => state.ready)

function applySession(data: SessionPayload) {
  state.user = { user_id: data.user_id, username: data.username }
  // 注意：不再 localStorage 存 token（后端已通过 HttpOnly Cookie 下发）
}

export async function register(username: string, password: string): Promise<SessionPayload> {
  const data = await apiFetch<SessionPayload>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  applySession(data)
  return data
}

export async function login(username: string, password: string): Promise<SessionPayload> {
  const data = await apiFetch<SessionPayload>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  applySession(data)
  return data
}

export async function logout(): Promise<void> {
  try {
    await apiFetch('/auth/logout', { method: 'POST' })
  } catch {
    // 即使后端清 Cookie 失败，前端也清本地态
  }
  state.user = null
}
