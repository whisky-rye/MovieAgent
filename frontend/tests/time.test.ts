import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fmtConvTime } from '../src/utils/time'

// 冻结系统时间，使「今天/昨天」判定确定性。
describe('fmtConvTime', () => {
  const NOW = new Date('2026-09-17T12:30:00')
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('null / undefined / 空串 返回空串', () => {
    expect(fmtConvTime(null)).toBe('')
    expect(fmtConvTime(undefined)).toBe('')
    expect(fmtConvTime('')).toBe('')
  })

  it('非法时间返回空串', () => {
    expect(fmtConvTime('not-a-date')).toBe('')
  })

  it('今天显示「今天 HH:MM」', () => {
    expect(fmtConvTime('2026-09-17T08:05:00')).toBe('今天 08:05')
  })

  it('昨天显示「昨天 HH:MM」', () => {
    expect(fmtConvTime('2026-09-16T23:59:00')).toBe('昨天 23:59')
  })

  it('今年显示「M月D日 HH:MM」', () => {
    expect(fmtConvTime('2026-03-05T09:00:00')).toBe('3月5日 09:00')
  })

  it('往年显示「YYYY年M月D日 HH:MM」', () => {
    expect(fmtConvTime('2025-12-31T20:00:00')).toBe('2025年12月31日 20:00')
  })

  it('接受 UTC ISO（Z）并按时区本地化解析为今天', () => {
    const d = new Date(NOW.getTime() - 3600_000)
    expect(fmtConvTime(d.toISOString())).toMatch(/^今天 \d{2}:\d{2}$/)
  })
})
