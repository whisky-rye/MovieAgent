// 会话 store：用 Vue reactive 替代 Pinia（满足"最少依赖"）。
// 负责：会话列表、当前会话、消息历史、增删改查；与后端 /sessions 对接。

import { reactive, computed } from 'vue'
import { apiFetch } from '../api/client'
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
    state.list = list
  } catch {
    state.list = []
    state.listError = '会话列表加载失败'
  } finally {
    state.loaded = true
    state.listLoading = false
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
  return sid
}

export async function selectSession(sid: string): Promise<void> {
  state.currentId = sid
  state.loading = true
  try {
    const data = await apiFetch<{ session_id: string; messages: unknown[] }>(`/sessions/${sid}`)
    state.messages = mapMessages(data.messages ?? [])
  } catch {
    state.messages = []
  } finally {
    state.loading = false
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
