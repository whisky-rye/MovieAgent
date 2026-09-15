// SSE 流式客户端：POST /recommend/stream，基于原生 fetch + ReadableStream 解析事件流。
// 支持 AbortController 中断；把 step/thinking/token/done/error 事件回调给调用方。
// 零第三方依赖（不引入 EventSource，因为 EventSource 仅支持 GET，而本接口是 POST）。

import { API_BASE } from './client'
import type { ChatStep, DonePayload } from '../types'

export interface StreamHandlers {
  onStep?: (step: ChatStep) => void
  onThinking?: (delta: string) => void
  onToken?: (delta: string) => void
  onDone?: (payload: DonePayload) => void
  onError?: (message: string) => void
}

interface SSEEvent {
  event: string
  data: string
}

// 解析单条 SSE 文本块（可能含 event:/data: 多行）
function parseSSEBlock(block: string): SSEEvent | null {
  let event = 'message'
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      data += (data ? '\n' : '') + line.slice(5).trim()
    }
  }
  if (!data) return null
  return { event, data }
}

function dispatch(ev: SSEEvent, h: StreamHandlers): boolean {
  let payload: unknown = null
  try {
    payload = JSON.parse(ev.data)
  } catch {
    payload = ev.data
  }
  switch (ev.event) {
    case 'step':
      h.onStep?.(payload as ChatStep)
      return false
    case 'thinking':
      h.onThinking?.((payload as { content?: string }).content ?? '')
      return false
    case 'token':
      h.onToken?.((payload as { content?: string }).content ?? '')
      return false
    case 'done':
      h.onDone?.(payload as DonePayload)
      return false
    case 'error':
      h.onError?.((payload as { message?: string }).message ?? '服务异常')
      return true // 终止
    default:
      return false
  }
}

export async function streamRecommend(
  query: string,
  sessionId: string | null,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem('ma_token')
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`

  let res: Response
  try {
    res = await fetch(`${API_BASE}/recommend/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, session_id: sessionId }),
      signal,
    })
  } catch (e: unknown) {
    if ((e as { name?: string }).name === 'AbortError') return
    handlers.onError?.('网络错误，请确认后端服务已启动')
    return
  }

  if (!res.ok || !res.body) {
    let msg = `请求失败（${res.status}）`
    try {
      const t = await res.text()
      if (t) {
        try {
          const j = JSON.parse(t)
          if (j.detail) msg = j.detail
        } catch {
          msg = t
        }
      }
    } catch {
      /* ignore */
    }
    handlers.onError?.(msg)
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // SSE 以空行(\n\n)分隔事件
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        if (!block.trim()) continue
        const ev = parseSSEBlock(block)
        if (ev && dispatch(ev, handlers)) return // error 事件，终止
      }
    }
    // 处理剩余缓冲区
    if (buffer.trim()) {
      const ev = parseSSEBlock(buffer)
      if (ev) dispatch(ev, handlers)
    }
  } catch (e: unknown) {
    if ((e as { name?: string }).name === 'AbortError') return
    handlers.onError?.('读取响应流失败')
  }
}
