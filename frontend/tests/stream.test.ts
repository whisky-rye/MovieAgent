import { describe, it, expect } from 'vitest'
import { classifyErrorPayload } from '../src/api/stream'

// 覆盖 M6 的 429/护栏/异常 在前端的差异化分类逻辑。
describe('classifyErrorPayload', () => {
  it('限流：提取 retry_after 并从文案解析秒数', () => {
    const e = classifyErrorPayload({ action: 'rate_limited', message: '请求过于频繁，请 5 秒后再试。' })
    expect(e.kind).toBe('rate_limited')
    expect(e.retry_after).toBe(5)
    expect(e.message).toMatch(/5 秒/)
  })

  it('限流：优先使用载荷中的 retry_after 数值', () => {
    const e = classifyErrorPayload({ action: 'rate_limited', message: '请稍后再试。', retry_after: 12 })
    expect(e.kind).toBe('rate_limited')
    expect(e.retry_after).toBe(12)
  })

  it('护栏：保留 action 与 category', () => {
    const e = classifyErrorPayload({ action: 'guardrail', category: 'offtopic', message: '这个我帮不上，换个电影话题吧。' })
    expect(e.kind).toBe('guardrail')
    expect(e.action).toBe('guardrail')
    expect(e.category).toBe('offtopic')
  })

  it('护栏：block / guide 同样归为 guardrail', () => {
    expect(classifyErrorPayload({ action: 'block', message: 'x' }).kind).toBe('guardrail')
    expect(classifyErrorPayload({ action: 'guide', message: 'x' }).kind).toBe('guardrail')
  })

  it('未知 action / 纯 message 归为 unknown', () => {
    const e = classifyErrorPayload({ message: '服务异常' })
    expect(e.kind).toBe('unknown')
    expect(e.message).toBe('服务异常')
  })

  it('空载荷给出兜底文案', () => {
    const e = classifyErrorPayload(null)
    expect(e.kind).toBe('unknown')
    expect(e.message).toBeTruthy()
  })
})
