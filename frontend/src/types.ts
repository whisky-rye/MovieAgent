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
  douban_id?: string // 豆瓣 subject id（用于跳转详情页，≠ 主键）
  imdb_id?: string   // IMDb title id（形如 tt1234567）
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
// 对话完成后由百炼视觉模型生成的 Top3 宣传海报（G-Poster）
export interface PosterInfo {
  url: string // 内联预览地址（<img> / <video> src）
  download_url: string // 触发浏览器下载的地址（?download=1）
  model: string // 实际触发的视觉模型
  model_family: 'image' | 'video' // 图片 or 视频
  media_type?: string
  placeholder?: boolean // 是否为断网占位图
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  items?: ChatItem[]
  steps?: ChatStep[]
  thinking?: string
  intent?: string
  sources?: Record<string, unknown>
  streaming?: boolean // 流式接收中
  error?: string // 错误展示文案（兼容旧字段）
  errorKind?: StreamError['kind'] // 错误类别，用于差异化展示/重试
  retryAfter?: number // 限流建议等待秒数（rate_limited 时）
  poster?: PosterInfo // 推荐海报（对话完成后异步生成，轮询回填）
}

// 结构化错误（M6：429/护栏/断线在前端可见且优雅）
export interface StreamError {
  kind: 'rate_limited' | 'guardrail' | 'network' | 'stream' | 'server' | 'http' | 'unknown'
  message: string
  retry_after?: number // 建议等待秒数
  action?: string // 后端 action（rate_limited / block / guide ...）
  category?: string // 护栏 category（sensitive:* / offtopic ...）
}

export interface SessionSummary {
  session_id: string
  title: string
  created_at: string | null
  updated_at: string | null
  preview: string
  shared?: boolean       // 是否已共享（公开只读）
  share_token?: string   // 共享令牌（已共享时存在）
}

// 公开只读会话（GET /share/{token}，无需登录）
export interface SharedSession {
  share_token: string
  title: string
  created_at: string | null
  updated_at: string | null
  messages: ChatMessage[]
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
  poster?: PosterInfo
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

