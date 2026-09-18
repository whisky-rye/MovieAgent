// @vitest-environment happy-dom
// O11 反馈三需求组件级验证（挂载真实 MessageItem.vue，断言 DOM 行为）：
//  1) 喜欢/不喜欢互斥：点喜欢再点不喜欢 → 仅不喜欢高亮，且二者不会同时 on；
//  2) 卡片布局：链接与反馈按钮同处 .links/.fb-group 结构；
//  3) 状态持久化与标识：本地/服务端恢复后，标题旁出现「已喜欢/已不喜欢」徽章并高亮。
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MessageItem from '../src/views/MessageItem.vue'
import * as fb from '../src/store/feedback'

// 避免真实 fetch 打到后端（submitFeedback 内部 postFeedback 失败已被 .catch 吞掉，不影响本地态）
vi.stubGlobal('fetch', vi.fn())

function makeItem(id: string, genres: string[] = ['剧情']) {
  return {
    id,
    title: '影片' + id,
    year: 2021,
    source: 'douban',
    source_label: '豆瓣',
    rating: 8.4,
    rating_count: 1200,
    country: '中国',
    genres,
    recall_source: 'vector',
    douban_id: id,
    imdb_id: null,
  }
}

function makeMessage() {
  return {
    role: 'assistant',
    content: '推荐如下',
    items: [makeItem('douban:1', ['剧情']), makeItem('douban:2', ['动作'])],
  } as any
}

describe('反馈 store 互斥逻辑（纯逻辑层）', () => {
  beforeEach(() => fb.resetFeedback())
  it('点喜欢再点不喜欢 → 仅保留不喜欢', () => {
    expect(fb.toggleFeedback('douban:1', 'like')).toBe('like')
    expect(fb.stateOf('douban:1')).toBe('like')
    expect(fb.toggleFeedback('douban:1', 'dislike')).toBe('dislike')
    expect(fb.stateOf('douban:1')).toBe('dislike')
    // 再点喜欢 → 切回喜欢（互斥，不会叠加）
    expect(fb.toggleFeedback('douban:1', 'like')).toBe('like')
    expect(fb.stateOf('douban:1')).toBe('like')
  })
  it('点同一状态再次 → 取消', () => {
    fb.toggleFeedback('douban:1', 'like')
    expect(fb.toggleFeedback('douban:1', 'like')).toBeNull()
    expect(fb.stateOf('douban:1')).toBeUndefined()
  })
})

describe('MessageItem 反馈 UI（组件层）', () => {
  beforeEach(() => {
    fb.resetFeedback()
    vi.restoreAllMocks()
  })

  it('需求1 互斥：喜欢→不喜欢，按钮高亮只留一个', async () => {
    const wrapper = mount(MessageItem, { props: { message: makeMessage() } })
    const card = wrapper.findAll('.item-card')[0]
    const like = card.find('.fb-btn.like')
    const dislike = card.find('.fb-btn.dislike')

    await like.trigger('click')
    await nextTick()
    expect(like.classes()).toContain('on')
    expect(dislike.classes()).not.toContain('on')

    await dislike.trigger('click')
    await nextTick()
    expect(like.classes()).not.toContain('on')
    expect(dislike.classes()).toContain('on')
  })

  it('需求3 标识：操作后标题旁出现「已不喜欢」徽章', async () => {
    const wrapper = mount(MessageItem, { props: { message: makeMessage() } })
    const card = wrapper.findAll('.item-card')[0]
    expect(card.find('.fb-flag').exists()).toBe(false) // 初始无标记

    await card.find('.fb-btn.dislike').trigger('click')
    await nextTick()
    const flag = card.find('.fb-flag')
    expect(flag.exists()).toBe(true)
    expect(flag.text()).toContain('已不喜欢')
  })

  it('需求3 持久化：本地已恢复的状态在挂载即显示徽章（无需再次点击）', () => {
    // 模拟「刷新/重进会话后从 GET /feedback/mine 恢复」：本地 store 已有该作品状态
    fb.submitFeedback('douban:1', 'like')
    const wrapper = mount(MessageItem, { props: { message: makeMessage() } })
    const card = wrapper.findAll('.item-card')[0]
    const flag = card.find('.fb-flag')
    expect(flag.exists()).toBe(true)
    expect(flag.text()).toContain('已喜欢')
    expect(card.find('.fb-btn.like').classes()).toContain('on')
  })

  it('需求2 布局：链接与反馈按钮同处 .links/.fb-group 结构', () => {
    const wrapper = mount(MessageItem, { props: { message: makeMessage() } })
    const card = wrapper.findAll('.item-card')[0]
    expect(card.find('.links').exists()).toBe(true)
    expect(card.find('.fb-group').exists()).toBe(true)
    // 反馈按钮组位于链接行内（DOM 同为 .links 子节点）
    expect(card.find('.links').find('.fb-group').exists()).toBe(true)
    expect(card.find('.ext.douban').exists()).toBe(true)
  })

  it('需求1 不同作品互不干扰：作品A点喜欢不影响作品B', async () => {
    const wrapper = mount(MessageItem, { props: { message: makeMessage() } })
    const cards = wrapper.findAll('.item-card')
    await cards[0].find('.fb-btn.like').trigger('click')
    await nextTick()
    expect(cards[0].find('.fb-btn.like').classes()).toContain('on')
    expect(cards[1].find('.fb-btn.like').classes()).not.toContain('on')
    expect(cards[1].find('.fb-flag').exists()).toBe(false)
  })
})
