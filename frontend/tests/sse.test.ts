import { describe, it, expect } from 'vitest'
import { parseSSEBlock, createSSEParser } from '../src/utils/sse'

describe('parseSSEBlock', () => {
  it('解析单条 event + data', () => {
    const ev = parseSSEBlock('event: token\ndata: {"content":"你好"}')
    expect(ev).toEqual({ event: 'token', data: '{"content":"你好"}' })
  })

  it('data 默认事件名为 message', () => {
    const ev = parseSSEBlock('data: hello')
    expect(ev?.event).toBe('message')
    expect(ev?.data).toBe('hello')
  })

  it('多行 data 用换行拼接', () => {
    const ev = parseSSEBlock('event: thinking\ndata: 第一行\ndata: 第二行')
    expect(ev?.data).toBe('第一行\n第二行')
  })

  it('无 data 的帧（注释/keep-alive）返回 null', () => {
    expect(parseSSEBlock(': keep-alive')).toBeNull()
    expect(parseSSEBlock('event: ping')).toBeNull()
  })

  it('空块返回 null', () => {
    expect(parseSSEBlock('')).toBeNull()
    expect(parseSSEBlock('   ')).toBeNull()
  })

  it('data 行允许带一个前导空格', () => {
    const ev = parseSSEBlock('data:  x')
    expect(ev?.data).toBe('x')
  })
})

describe('createSSEParser（跨 chunk 分片）', () => {
  it('一个 chunk 含多帧全部解析', () => {
    const p = createSSEParser()
    const events = p.push('event: step\ndata: {"a":1}\n\nevent: token\ndata: {"content":"hi"}\n\n')
    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ event: 'step', data: '{"a":1}' })
    expect(events[1]).toEqual({ event: 'token', data: '{"content":"hi"}' })
  })

  it('一帧被拆到两个 chunk 仍能正确拼接', () => {
    const p = createSSEParser()
    const e1 = p.push('event: token\ndata: {"con')
    const e2 = p.push('tent":"你好"}\n\n')
    expect(e1).toHaveLength(0) // 第一块还没遇到 \n\n
    expect(e2).toHaveLength(1)
    expect(e2[0]).toEqual({ event: 'token', data: '{"content":"你好"}' })
  })

  it('帧结尾无空行时用 flush 兜底', () => {
    const p = createSSEParser()
    p.push('event: done\ndata: {"ok":true}\n') // 缺最后的 \n\n
    const flushed = p.flush()
    expect(flushed).toHaveLength(1)
    expect(flushed[0]).toEqual({ event: 'done', data: '{"ok":true}' })
  })

  it('连续推送 + 含注释行不产出事件', () => {
    const p = createSSEParser()
    const all = [
      ...p.push(': comment\n\nevent: a\ndata: 1\n\n'),
      ...p.push('event: b\ndata: 2\n\n'),
    ]
    expect(all.map((e) => e.event)).toEqual(['a', 'b'])
  })

  it('真实后端错误帧（结构化 JSON）可解析', () => {
    const p = createSSEParser()
    const events = p.push(
      'event: error\ndata: {"action":"rate_limited","message":"请求过于频繁，请 5 秒后再试。"}\n\n',
    )
    expect(events).toHaveLength(1)
    expect(events[0].event).toBe('error')
    const payload = JSON.parse(events[0].data)
    expect(payload.action).toBe('rate_limited')
    expect(payload.message).toMatch(/5 秒/)
  })
})
