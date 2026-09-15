// 轻量 UI 状态：用 Vue reactive 替代 vue-router（满足"最少依赖"原则）。
// 仅控制主区域在「对话」与「记忆」两个视图间切换（条件渲染，无路由表）。

import { reactive, computed } from 'vue'

export type MainView = 'chat' | 'memory'

interface UiState {
  view: MainView
}

const state = reactive<UiState>({ view: 'chat' })

export const currentView = computed(() => state.view)

export function setView(v: MainView): void {
  state.view = v
}
