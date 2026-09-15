import { describe, it, expect } from 'vitest'
import { doubanUrl, imdbUrl } from '../src/utils/extlinks'

describe('extlinks', () => {
  it('豆瓣 subject 链接正确拼接', () => {
    expect(doubanUrl('1291546')).toBe('https://movie.douban.com/subject/1291546')
  })
  it('IMDb title 链接正确拼接（含 tt 前缀）', () => {
    expect(imdbUrl('tt4561226')).toBe('https://www.imdb.com/title/tt4561226')
  })
  it('缺失 id 返回 null，不渲染链接', () => {
    expect(doubanUrl('')).toBeNull()
    expect(doubanUrl(null)).toBeNull()
    expect(doubanUrl(undefined)).toBeNull()
    expect(imdbUrl('  ')).toBeNull()
  })
  it('容忍数字型 id 与空白', () => {
    expect(doubanUrl(1291546)).toBe('https://movie.douban.com/subject/1291546')
    expect(imdbUrl('  tt123 ')).toBe('https://www.imdb.com/title/tt123')
  })
})
