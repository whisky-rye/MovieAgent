// 三层记忆（M5）· 长期偏好 store：用 Vue reactive 替代 Pinia（满足"最少依赖"原则）。
// 与后端 /memory/* 对接，读写 movie_agent.user_preferences（或 file 兜底），与 Agent 共用同一份数据源。

import { reactive, computed } from 'vue'
import { getPreferences, putPreferences, deletePreferences, getHistory } from '../api/memory'
import type { Preferences } from '../types'

interface MemoryState {
  preferences: Preferences
  history: unknown[]
  loading: boolean
  saving: boolean
  error: string | null
}

const state = reactive<MemoryState>({
  preferences: { genres: [], note: '' },
  history: [],
  loading: false,
  saving: false,
  error: null,
})

export const memoryPreferences = computed(() => state.preferences)
export const memoryHistory = computed(() => state.history)
export const memoryLoading = computed(() => state.loading)
export const memorySaving = computed(() => state.saving)
export const memoryError = computed(() => state.error)

export async function loadMemory(): Promise<void> {
  state.loading = true
  state.error = null
  try {
    const [prefs, hist] = await Promise.all([getPreferences(), getHistory()])
    state.preferences = prefs.preferences ?? { genres: [], note: '' }
    state.history = hist.history ?? []
  } catch (e) {
    state.error = (e as Error).message || '加载记忆失败'
  } finally {
    state.loading = false
  }
}

// 追加偏好类型（去重合并由后端保证）
export async function addGenre(genre: string): Promise<void> {
  const g = genre.trim()
  if (!g) return
  const cur = (state.preferences.genres ?? []).slice()
  if (cur.includes(g)) return
  await savePreferences({ genres: [g] })
}

// 删除偏好类型（后端按 remove_genres 剔除）
export async function removeGenre(genre: string): Promise<void> {
  await savePreferences({ remove_genres: [genre] })
}

// 覆盖写备注
export async function setNote(note: string): Promise<void> {
  await savePreferences({ note })
}

async function savePreferences(patch: { genres?: string[]; remove_genres?: string[]; note?: string }): Promise<void> {
  state.saving = true
  state.error = null
  try {
    const res = await putPreferences(patch)
    state.preferences = res.preferences ?? { genres: [], note: '' }
  } catch (e) {
    state.error = (e as Error).message || '保存偏好失败'
    throw e
  } finally {
    state.saving = false
  }
}

// 清空长期偏好（保留 history）
export async function clearPreferences(): Promise<void> {
  state.saving = true
  state.error = null
  try {
    const res = await deletePreferences()
    state.preferences = res.preferences ?? { genres: [], note: '' }
  } catch (e) {
    state.error = (e as Error).message || '清空偏好失败'
  } finally {
    state.saving = false
  }
}
