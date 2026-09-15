// 轻量鉴权 store：用 Vue 的 reactive + computed 替代 Pinia（满足"最少依赖"原则）。
// 单一实例、持久化到 localStorage，应用启动时调用 initAuth() 自动续期校验。

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
  token: string | null
  ready: boolean // 启动续期校验是否完成（避免首帧闪登录页）
}

const state = reactive<AuthState>({
  user: null,
  token: localStorage.getItem('ma_token'),
  ready: false,
})

function applySession(data: SessionPayload) {
  state.token = data.token
  state.user = { user_id: data.user_id, username: data.username }
  localStorage.setItem('ma_token', data.token)
}

export const authUser = computed(() => state.user)
export const isLoggedIn = computed(() => !!state.user)
export const authReady = computed(() => state.ready)

// 应用启动：若本地有 token 则调 /auth/me 校验有效性，失效则清理。
export async function initAuth(): Promise<void> {
  if (!state.token) {
    state.ready = true
    return
  }
  try {
    const me = await apiFetch<AuthUser>('/auth/me')
    state.user = me
  } catch {
    logout()
  } finally {
    state.ready = true
  }
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

export function logout(): void {
  state.user = null
  state.token = null
  localStorage.removeItem('ma_token')
}
