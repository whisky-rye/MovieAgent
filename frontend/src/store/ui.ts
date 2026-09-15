// 轻量 UI 状态：用 Vue reactive 替代 vue-router（满足"最少依赖"原则）。
// 仅控制主区域在「对话」与「记忆」两个视图间切换（条件渲染，无路由表），
// 以及移动端侧边栏抽屉的开关。

import { reactive, computed } from 'vue'

export type MainView = 'chat' | 'memory'

interface UiState {
  view: MainView
  sidebarOpen: boolean
}

const state = reactive<UiState>({ view: 'chat', sidebarOpen: false })

export const currentView = computed(() => state.view)

export function setView(v: MainView): void {
  state.view = v
}

// 移动端侧边栏抽屉
export const sidebarOpen = computed(() => state.sidebarOpen)
export function toggleSidebar(): void {
  state.sidebarOpen = !state.sidebarOpen
}
export function closeSidebar(): void {
  state.sidebarOpen = false
}
