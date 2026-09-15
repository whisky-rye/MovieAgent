import { describe, it, expect } from 'vitest'
import { normalizeThinking } from '../src/store/sessions'

// 后端 thinking 可能是对象（{route, generate}，P2.13 思考可视化）或字符串，
// 前端 ChatMessage.thinking 约定为字符串。normalizeThinking 在加载历史会话时归一化。
describe('normalizeThinking', () => {
  it('null / undefined 返回 undefined', () => {
    expect(normalizeThinking(null)).toBeUndefined()
    expect(normalizeThinking(undefined)).toBeUndefined()
  })

  it('字符串原样返回', () => {
    expect(normalizeThinking('我在思考…')).toBe('我在思考…')
  })

  it('对象按字符串值拼接（过滤空串）', () => {
    const obj = { route: '已规划步骤', generate: '推导中…', empty: '' }
    expect(normalizeThinking(obj)).toBe('已规划步骤\n\n推导中…')
  })

  it('对象全是空串时返回空串', () => {
    expect(normalizeThinking({ a: '', b: '' })).toBe('')
  })

  it('数字等非字符串标量转为字符串', () => {
    expect(normalizeThinking(123)).toBe('123')
  })
})
