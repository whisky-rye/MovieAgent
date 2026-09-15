// 三层记忆（M5）· 长期偏好/历史的 API 客户端。
// 对接后端 /memory/*（G4）：长期偏好跨会话共享，与 Agent remember_preference 共用同一份数据源。

import { apiFetch } from './client'
import type { Preferences, PreferencesResp, HistoryResp, PrefPatch } from '../types'

export function getPreferences(): Promise<PreferencesResp> {
  return apiFetch<PreferencesResp>('/memory/preferences')
}

export function putPreferences(patch: PrefPatch): Promise<PreferencesResp> {
  return apiFetch<PreferencesResp>('/memory/preferences', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })
}

export function deletePreferences(): Promise<PreferencesResp> {
  return apiFetch<PreferencesResp>('/memory/preferences', { method: 'DELETE' })
}

export function getHistory(): Promise<HistoryResp> {
  return apiFetch<HistoryResp>('/memory/history')
}
