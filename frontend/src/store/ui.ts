// 轻量 UI 状态：用 Vue reactive 替代 vue-router（满足"最少依赖"原则）。
// 仅控制主区域在「对话」与「记忆」两个视图间切换（条件渲染，无路由表），
// 以及移动端侧边栏抽屉的开关。

import { reactive, computed } from 'vue'

export type MainView = 'chat' | 'memory'
export type Theme = 'light' | 'dark'

const THEME_KEY = 'ma_theme'

function readInitialTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY)
    if (t === 'light' || t === 'dark') return t
  } catch {
    /* localStorage 不可用时回退默认 */
  }
  return 'light'
}

// 模块加载即套用主题，避免首屏闪烁（FOUC）
function applyTheme(t: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', t)
  }
}

interface UiState {
  view: MainView
  sidebarOpen: boolean
  theme: Theme
}

const state = reactive<UiState>({
  view: 'chat',
  sidebarOpen: false,
  theme: readInitialTheme(),
})

// 立即套用初始主题（在 #app 挂载之前）
applyTheme(state.theme)

export const currentView = computed(() => state.view)

export function setView(v: MainView): void {
  state.view = v
}

// ---- 主题切换 ----
export const theme = computed(() => state.theme)

export function toggleTheme(): void {
  setTheme(state.theme === 'light' ? 'dark' : 'light')
}

export function setTheme(t: Theme): void {
  state.theme = t
  applyTheme(t)
  try {
    localStorage.setItem(THEME_KEY, t)
  } catch {
    /* 忽略持久化失败 */
  }
}

// 移动端侧边栏抽屉
export const sidebarOpen = computed(() => state.sidebarOpen)
export function toggleSidebar(): void {
  state.sidebarOpen = !state.sidebarOpen
}
export function closeSidebar(): void {
  state.sidebarOpen = false
}
