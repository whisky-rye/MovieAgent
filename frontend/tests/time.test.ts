import { describe, it, expect } from 'vitest'
import { fmtConvTime } from '../src/utils/time'

// 绝对时间格式：YYYY-MM-DD HH:MM（本地时区）。
describe('fmtConvTime (绝对时间)', () => {
  it('null / undefined / 空串 返回空串', () => {
    expect(fmtConvTime(null)).toBe('')
    expect(fmtConvTime(undefined)).toBe('')
    expect(fmtConvTime('')).toBe('')
  })

  it('非法时间返回空串', () => {
    expect(fmtConvTime('not-a-date')).toBe('')
  })

  it('本地时间字符串按绝对格式输出', () => {
    // 无 Z 后缀按本地时区解析，输出与输入日历一致
    expect(fmtConvTime('2026-09-17T08:05:00')).toBe('2026-09-17 08:05')
  })

  it('今年其它日期同样绝对输出', () => {
    expect(fmtConvTime('2026-03-05T09:00:00')).toBe('2026-03-05 09:00')
  })

  it('往年同样绝对输出', () => {
    expect(fmtConvTime('2025-12-31T20:00:00')).toBe('2025-12-31 20:00')
  })

  it('补零：个位月/日/时/分均两位', () => {
    expect(fmtConvTime('2026-01-02T03:04:00')).toBe('2026-01-02 03:04')
  })

  it('接受 UTC ISO（Z）并按时区本地化解析', () => {
    // 仅校验形状 YYYY-MM-DD HH:MM，不依赖具体时区偏移
    expect(fmtConvTime('2026-09-17T00:05:00Z')).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
  })
})
