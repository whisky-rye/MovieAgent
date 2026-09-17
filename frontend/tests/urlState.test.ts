import { describe, it, expect } from 'vitest'
import { parseHash, buildHash } from '../src/urlState'

// 纯函数测试：URL hash 与 {view, sid} 的互转（readUrlState/writeUrlState 的 DOM 包装由浏览器行为保证）。
describe('urlState parse/build', () => {
  it('空 hash 回退默认 chat + 无 sid', () => {
    expect(parseHash('')).toEqual({ view: 'chat', sid: null })
    expect(parseHash('#')).toEqual({ view: 'chat', sid: null })
  })

  it('解析 chat + sid', () => {
    expect(parseHash('#view=chat&sid=abc123')).toEqual({ view: 'chat', sid: 'abc123' })
  })

  it('解析 memory + sid', () => {
    expect(parseHash('#view=memory&sid=xyz')).toEqual({ view: 'memory', sid: 'xyz' })
  })

  it('view 大小写不敏感', () => {
    expect(parseHash('#view=MEMORY').view).toBe('memory')
  })

  it('无 view 时回退 chat', () => {
    expect(parseHash('#sid=only')).toEqual({ view: 'chat', sid: 'only' })
  })

  it('非法 view 回退 chat', () => {
    expect(parseHash('#view=foobar&sid=x').view).toBe('chat')
  })

  it('构建默认态仅含 view', () => {
    expect(buildHash('chat', null)).toBe('#view=chat')
  })

  it('构建含 sid', () => {
    expect(buildHash('memory', 's1')).toBe('#view=memory&sid=s1')
  })

  it('构建对 sid 做 encode 且可往返解析', () => {
    const h = buildHash('chat', 'a b/c')
    expect(parseHash(h).sid).toBe('a b/c')
  })
})
