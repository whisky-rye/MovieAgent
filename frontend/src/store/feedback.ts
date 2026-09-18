// O11 反馈状态 store：全局共享「我对某作品点过什么」。
//
// 为什么要有它（而不是把状态留在 MessageItem 内部）：
//  1. 同一作品可能出现在多条消息 / 多次推荐里，状态必须一致；
//  2. 刷新页面、切换会话、重进列表后仍要显示历史操作 —— 内存态会被清空，
//     故启动时从后端 movie_agent.user_feedback 表拉取（GET /feedback/mine）做恢复；
//  3. 喜欢 / 不喜欢互斥：同一作品只能处于其一，切换或取消都在这里收口。

import { reactive, computed } from 'vue'
import { fetchMyFeedback, postFeedback, type FeedbackState } from '../api/client'

interface FeedbackStoreState {
  map: Record<string, FeedbackState>
  loaded: boolean
}

const state = reactive<FeedbackStoreState>({ map: {}, loaded: false })

export const feedbackLoaded = computed(() => state.loaded)

// 查询某作品当前状态（undefined = 未操作）
export function stateOf(itemId: string): FeedbackState | undefined {
  return state.map[itemId]
}

// 互斥切换：点击「另一个」→ 直接切过去；再点「同一个」→ 取消（撤销）。
// 返回切换后的状态，null 表示已取消。
export function toggleFeedback(itemId: string, signal: FeedbackState): FeedbackState | null {
  const next: FeedbackState | null = state.map[itemId] === signal ? null : signal
  if (next) state.map[itemId] = next
  else delete state.map[itemId]
  return next
}

// 上报 + 本地乐观更新：先改本地保证点击即时可见，服务端失败时静默
// （不阻塞浏览；下一次 loadMyFeedback 会以服务端为准纠正）。
export function submitFeedback(
  itemId: string,
  signal: FeedbackState,
  genres?: string[],
  userId?: string,
): FeedbackState | null {
  const next = toggleFeedback(itemId, signal)
  // next 为 null 时发 cancel，服务端清掉该作品的显式赞/踩
  postFeedback({ item_id: itemId, signal: next ?? 'cancel', genres, user_id: userId }).catch(() => {})
  return next
}

// 隐式信号（点击卡片）：仅埋点，不影响赞踩状态；同一组件内同一作品只记一次
export function submitClick(itemId: string, genres?: string[], userId?: string): void {
  postFeedback({ item_id: itemId, signal: 'click', genres, user_id: userId }).catch(() => {})
}

// 从服务端恢复历史状态（登录后 / 刷新后调用）。以服务端为准整体替换。
// userId 显式传入可确保恢复到「同一用户」的历史（避免落到 default）。
export async function loadMyFeedback(userId?: string): Promise<void> {
  try {
    const data = await fetchMyFeedback(userId)
    for (const k of Object.keys(state.map)) delete state.map[k]
    Object.assign(state.map, data.items ?? {})
    state.loaded = true
  } catch {
    /* 反馈状态拉取失败不影响浏览 */
  }
}

// 退出登录时清空，避免下一个账号看到上一个账号的标记
export function resetFeedback(): void {
  for (const k of Object.keys(state.map)) delete state.map[k]
  state.loaded = false
}
