// 前后端共用的数据模型类型（与后端 api/main.py / agent/session_store.py 的响应结构对齐）。

export interface ChatItem {
  id: string
  source: string
  source_label: string
  title: string
  year: number | null
  genres: string[]
  country: string | null
  rating: number
  rating_count: number | null
  recall_source: string | null
  score: number
}

export interface ChatStep {
  order: number
  node: string
  kind: string
  summary: string
  detail?: string
  elapsed_ms?: number
  thinking?: string
}

// 单条对话消息（前端渲染用；user 仅含 content，assistant 含答案与候选等）
export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  items?: ChatItem[]
  steps?: ChatStep[]
  thinking?: string
  intent?: string
  sources?: Record<string, unknown>
  streaming?: boolean // 流式接收中
  error?: string
}

export interface SessionSummary {
  session_id: string
  title: string
  created_at: string | null
  updated_at: string | null
  preview: string
}

// /recommend/stream 的 done 事件载荷
export interface DonePayload {
  request_id: string
  intent: string
  answer: string
  items: ChatItem[]
  sources: Record<string, unknown>
  steps: ChatStep[]
  thinking: Record<string, unknown>
  elapsed_ms: number
}

// ---- 三层记忆（M5）----
// 长期偏好：跨会话共享，落后端 get_memory_store()（user_id 主键）
export interface Preferences {
  genres?: string[]
  note?: string
  [key: string]: unknown
}

export interface PreferencesResp {
  user_id: string
  preferences: Preferences
}

export interface HistoryResp {
  user_id: string
  history: unknown[]
}

// 记忆写入补丁（对齐 api/routes/memory.py 的 PrefPutReq）
export interface PrefPatch {
  genres?: string[]
  remove_genres?: string[]
  note?: string
  preferences?: Record<string, unknown>
}

