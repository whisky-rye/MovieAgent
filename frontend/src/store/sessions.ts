// 会话 store：用 Vue reactive 替代 Pinia（满足"最少依赖"）。
// 负责：会话列表、当前会话、消息历史、增删改查；与后端 /sessions 对接。

import { reactive, computed } from 'vue'
import { apiFetch, fetchPoster, API_BASE, type PosterStatus } from '../api/client'
import { shareSession as apiShare, unshareSession as apiUnshare } from '../api/share'
import { writeUrlState, readUrlView } from '../urlState'
import type { ChatMessage, SessionSummary } from '../types'

interface SessionState {
  list: SessionSummary[]
  currentId: string | null
  messages: ChatMessage[]
  loading: boolean
  loaded: boolean
  listLoading: boolean
  listError: string | null
}

const state = reactive<SessionState>({
  list: [],
  currentId: null,
  messages: [],
  loading: false,
  loaded: false,
  listLoading: false,
  listError: null,
})

export const sessionList = computed(() => state.list)
export const currentSessionId = computed(() => state.currentId)
export const currentMessages = computed(() => state.messages)
export const sessionsLoading = computed(() => state.loading)
export const sessionsListLoading = computed(() => state.listLoading)
export const sessionsListError = computed(() => state.listError)

// 后端 thinking 可能是对象（{route, generate}，P2.13 思考可视化），
// 而前端 ChatMessage.thinking 约定为字符串。在加载历史会话时归一化，
// 否则 MessageItem 里 message.thinking.trim() 会因对象而抛错、整页渲染中断。
export function normalizeThinking(t: unknown): string | undefined {
  if (t == null) return undefined
  if (typeof t === 'string') return t
  if (typeof t === 'object') {
    return Object.values(t as Record<string, unknown>)
      .filter((v): v is string => typeof v === 'string' && v.length > 0)
      .join('\n\n')
  }
  return String(t)
}

// 把后端 /sessions/{id} 返回的 messages 映射为前端 ChatMessage
function mapMessages(msgs: unknown[]): ChatMessage[] {
  return (msgs ?? []).map((m) => {
    const msg = m as Record<string, unknown>
    return {
      role: (msg.role as 'user' | 'assistant') ?? 'user',
      content: (msg.content as string) ?? '',
      items: (msg.items as ChatMessage['items']) ?? undefined,
      steps: (msg.steps as ChatMessage['steps']) ?? undefined,
      thinking: normalizeThinking(msg.thinking),
      intent: (msg.intent as string) ?? undefined,
      sources: (msg.sources as Record<string, unknown>) ?? undefined,
    }
  })
}

export async function loadSessions(): Promise<void> {
  state.listLoading = true
  state.listError = null
  try {
    const list = await apiFetch<SessionSummary[]>('/sessions')
    // 后端已附带 shared / share_token 字段，直接保留即可
    state.list = list
  } catch {
    state.list = []
    state.listError = '会话列表加载失败'
  } finally {
    state.loaded = true
    state.listLoading = false
  }
}

// —— 会话共享（G-Share）——
export async function shareSession(sid: string): Promise<string> {
  const data = await apiShare(sid)
  const item = state.list.find((s) => s.session_id === sid)
  if (item) {
    item.shared = true
    item.share_token = data.share_token
  }
  return data.share_url
}

export async function unshareSession(sid: string): Promise<void> {
  await apiUnshare(sid)
  const item = state.list.find((s) => s.session_id === sid)
  if (item) {
    item.shared = false
    item.share_token = undefined
  }
}

export async function newSession(): Promise<string> {
  const data = await apiFetch<{ session_id: string }>('/sessions', {
    method: 'POST',
    body: JSON.stringify({ title: '新会话' }),
  })
  const sid = data.session_id
  state.list = [{ session_id: sid, title: '新会话', created_at: null, updated_at: null, preview: '' }, ...state.list]
  state.currentId = sid
  state.messages = []
  // 固定链接：新建会话即写入 URL，刷新后保持在该会话
  writeUrlState(readUrlView(), sid)
  return sid
}

export async function selectSession(sid: string): Promise<void> {
  state.currentId = sid
  // 固定链接：选中会话即写入 URL，刷新后恢复选中态
  writeUrlState(readUrlView(), sid)
  state.loading = true
  try {
    const data = await apiFetch<{ session_id: string; messages: unknown[] }>(`/sessions/${sid}`)
    state.messages = mapMessages(data.messages ?? [])
  } catch {
    state.messages = []
  } finally {
    state.loading = false
  }
  // G-Poster：刷新/切换会话后，重新恢复该会话已生成的海报（manifest 持久化在后端，刷新不丢失）。
  // 注意：不 await——海报恢复最多轮询数十秒，若阻塞在此会让「正在加载会话…」长时间转圈（踩过坑）。
  restorePoster(sid)
}

// G-Poster：把后端 manifest 中已就绪的海报回填到「最后一条助手消息」，使刷新/切换后海报持续显示。
// 后端返回的是 host 相对路径，需拼上 API_BASE 才能指向真实后端（否则开发环境会去 Vite :5173 取 SPA HTML）。
// 用自增 token 防竞态：快速切换会话时，旧会话的迟回包不得挂到新会话的消息上。
let posterRestoreToken = 0
async function restorePoster(sid: string): Promise<void> {
  const token = ++posterRestoreToken
  const abs = (p?: string) => (!p ? p : /^https?:\/\//i.test(p) ? p : API_BASE + p)
  // 仅在「pending」（生成进行中）时轮询等待；ready 直接回填；
  // none（从未生成/manifest 被清）/ error（生成失败）都是终态，立即返回——
  // 绝不能对未知状态死轮询（曾导致会话加载卡 60s，见 2026-09-17 修复）。
  let st: PosterStatus | null = null
  for (let attempt = 0; attempt < 30; attempt++) {
    try {
      st = await fetchPoster(sid)
    } catch {
      return
    }
    if (st.status === 'ready') break
    if (st.status !== 'pending') return
    if (token !== posterRestoreToken) return // 已切换到其他会话，放弃本次恢复
    await new Promise((r) => setTimeout(r, 2000))
  }
  if (!st || st.status !== 'ready' || !st.url) return
  if (token !== posterRestoreToken || state.currentId !== sid) return
  const msgs = state.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') {
      msgs[i].poster = {
        url: abs(st.url) ?? '',
        download_url: abs(st.download_url) ?? '',
        model: st.model ?? '',
        model_family: st.model_family ?? 'image',
        media_type: st.media_type,
        placeholder: st.placeholder,
      }
      break
    }
  }
}

export async function renameSession(sid: string, title: string): Promise<void> {
  try {
    await apiFetch(`/sessions/${sid}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    })
  } catch {
    /* 静默失败，UI 仍乐观更新 */
  }
  const item = state.list.find((s) => s.session_id === sid)
  if (item) item.title = title
}

export async function deleteSession(sid: string): Promise<void> {
  try {
    await apiFetch(`/sessions/${sid}`, { method: 'DELETE' })
  } catch {
    /* 静默失败 */
  }
  state.list = state.list.filter((s) => s.session_id !== sid)
  if (state.currentId === sid) {
    state.currentId = null
    state.messages = []
    // 固定链接：删除当前会话后清掉 URL 中的 sid
    writeUrlState(readUrlView(), null)
  }
}

// 本地追加一条消息（发送/接收时用，避免每次都打接口）
export function pushMessage(msg: ChatMessage): void {
  state.messages.push(msg)
}

export function updateLastAssistant(updater: (m: ChatMessage) => void): void {
  const msgs = state.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') {
      updater(msgs[i])
      return
    }
  }
}

export function clearCurrent(): void {
  state.messages = []
}

// 重试时裁掉「最后一条用户消息之后的所有内容」（即上一次失败的助手占位），
// 避免历史里残留一条报错气泡。
export function truncateAfter(index: number): void {
  if (index < 0) return
  state.messages.splice(index + 1)
}
