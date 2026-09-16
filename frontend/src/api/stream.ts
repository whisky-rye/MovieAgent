// SSE 流式客户端：POST /recommend/stream，基于原生 fetch + ReadableStream 解析事件流。
// 支持 AbortController 中断；把 step/thinking/token/done/error 事件回调给调用方。
// 零第三方依赖（不引入 EventSource，因为 EventSource 仅支持 GET，而本接口是 POST）。
//
// M6 增强：
//  - onConnecting / onFirstToken 钩子（加载态 / 连接态）
//  - 结构化错误 StreamError：限流(rate_limited+Retry-After)、护栏(guardrail)、
//    网络断线(network)、流中断(stream)、服务端(server/http)
//  - 解析器改用 utils/sse 的 createSSEParser，正确处理跨 chunk 分片

import { API_BASE, getCookie } from './client'
import { createSSEParser } from '../utils/sse'
import type { ChatStep, DonePayload, StreamError } from '../types'

export interface StreamHandlers {
  onStep?: (step: ChatStep) => void
  onThinking?: (delta: string) => void
  onToken?: (delta: string) => void
  onDone?: (payload: DonePayload) => void
  onError?: (err: StreamError) => void
  onConnecting?: () => void
  onFirstToken?: () => void
}

// 把后端 event:error 的载荷分类成结构化错误（导出以便单测覆盖）
export function classifyErrorPayload(payload: unknown): StreamError {
  const p = (payload ?? {}) as Record<string, unknown>
  const action = typeof p.action === 'string' ? p.action : undefined
  const message = typeof p.message === 'string' ? p.message : ''
  if (action === 'rate_limited') {
    const m = message.match(/(\d+)\s*秒/)
    const retry_after =
      typeof p.retry_after === 'number'
        ? p.retry_after
        : m
          ? Number(m[1])
          : undefined
    return {
      kind: 'rate_limited',
      message: message || '请求过于频繁，请稍后再试。',
      retry_after,
      action,
    }
  }
  if (action === 'guardrail' || action === 'block' || action === 'guide') {
    return {
      kind: 'guardrail',
      message: message || '该内容暂不支持，换个电影相关的话题试试？',
      action,
      category: typeof p.category === 'string' ? p.category : undefined,
    }
  }
  return { kind: 'unknown', message: message || '服务异常，请稍后重试。' }
}

export async function streamRecommend(
  query: string,
  sessionId: string | null,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  // 鉴权走 httpOnly Cookie（credentials:'include' 自动携带），CSRF 双提交从可读 Cookie 取 ma_csrf。
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const csrf = getCookie('ma_csrf')
  if (csrf) headers['X-CSRF-Token'] = csrf

  handlers.onConnecting?.()
  let res: Response
  try {
    res = await fetch(`${API_BASE}/recommend/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, session_id: sessionId }),
      credentials: 'include',
      signal,
    })
  } catch (e: unknown) {
    if ((e as { name?: string }).name === 'AbortError') return
    handlers.onError?.({
      kind: 'network',
      message: '网络错误：无法连接后端（请确认 http://localhost:8000 已启动）。',
    })
    return
  }

  // 非 2xx：可能是 429（带 Retry-After 头）或 5xx
  if (!res.ok || !res.body) {
    const status = res.status
    const retryAfterHeader = res.headers?.get?.('Retry-After')
    const retry_after = retryAfterHeader ? Number(retryAfterHeader) || undefined : undefined
    let message = ''
    try {
      const t = await res.text()
      if (t) {
        try {
          const j = JSON.parse(t)
          if (j.detail) message = j.detail
        } catch {
          message = t
        }
      }
    } catch {
      /* ignore */
    }
    if (status === 429) {
      const m = message.match(/(\d+)/)
      handlers.onError?.({
        kind: 'rate_limited',
        message: message || '请求过于频繁，请稍后再试。',
        retry_after: retry_after ?? (m ? Number(m[1]) : undefined),
      })
    } else if (status >= 500) {
      handlers.onError?.({
        kind: 'server',
        message: message || `服务内部错误（${status}），请稍后重试。`,
      })
    } else {
      handlers.onError?.({
        kind: 'http',
        message: message || `请求失败（${status}）。`,
      })
    }
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  const parser = createSSEParser()
  let firstTokenFired = false

  const dispatch = (ev: { event: string; data: string }) => {
    let payload: unknown = null
    try {
      payload = JSON.parse(ev.data)
    } catch {
      payload = ev.data
    }
    switch (ev.event) {
      case 'step':
        handlers.onStep?.(payload as ChatStep)
        return false
      case 'thinking':
        if (!firstTokenFired) {
          firstTokenFired = true
          handlers.onFirstToken?.()
        }
        handlers.onThinking?.((payload as { content?: string }).content ?? '')
        return false
      case 'token':
        if (!firstTokenFired) {
          firstTokenFired = true
          handlers.onFirstToken?.()
        }
        handlers.onToken?.((payload as { content?: string }).content ?? '')
        return false
      case 'done':
        if (!firstTokenFired) handlers.onFirstToken?.()
        handlers.onDone?.(payload as DonePayload)
        return false
      case 'error':
        handlers.onError?.(classifyErrorPayload(payload))
        return true // 终止
      default:
        return false
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      const events = parser.push(decoder.decode(value, { stream: true }))
      for (const ev of events) {
        if (dispatch(ev)) return // error 事件，终止
      }
    }
    for (const ev of parser.flush()) {
      if (dispatch(ev)) return
    }
  } catch (e: unknown) {
    if ((e as { name?: string }).name === 'AbortError') return
    handlers.onError?.({
      kind: 'stream',
      message: '读取响应流中断，可能是网络波动。',
    })
  }
}
