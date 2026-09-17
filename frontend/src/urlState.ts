// 会话列表固定链接：用 URL hash 承载「当前视图 + 选中会话」，F5 刷新后由 Home 读取恢复，
// 避免刷新后丢失列表状态、跳回首页。纯函数 (parseHash/buildHash) 与 DOM 包装分离，便于单测。

export type AppView = 'chat' | 'memory'

export interface UrlState {
  view: AppView
  sid: string | null
}

// 纯解析：从任意 hash 字符串中提取 {view, sid}（无匹配则回退默认）。
// 用 URLSearchParams 解析，确保与 buildHash 的编码互逆（+ 视为空格、%2F 还原为 /）。
export function parseHash(hash: string): UrlState {
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const rawView = params.get('view')?.toLowerCase()
  const view: AppView = rawView === 'memory' ? 'memory' : 'chat'
  const sid = params.get('sid')
  return { view, sid: sid ?? null }
}

// 纯构建：根据 {view, sid} 生成 hash 串（含前导 #）
export function buildHash(view: AppView, sid: string | null): string {
  const params = new URLSearchParams()
  params.set('view', view)
  if (sid) params.set('sid', sid)
  return '#' + params.toString()
}

// 浏览器环境读取：无 window（SSR/测试）时回退默认
export function readUrlState(): UrlState {
  if (typeof window === 'undefined') return { view: 'chat', sid: null }
  return parseHash(window.location.hash)
}

// 便捷访问器：store 中按需取 view / sid，避免重复解析
export function readUrlView(): AppView {
  return readUrlState().view
}
export function readUrlSid(): string | null {
  return readUrlState().sid
}

// 浏览器环境写入：用 replaceState 更新 hash（不产生_history 记录，F5 保留）。
// 默认态（chat + 无选中会话）且当前无 hash 时不写，保持 URL 干净；仅当发生真实导航后才落盘。
export function writeUrlState(view: AppView, sid: string | null): void {
  if (typeof window === 'undefined') return
  if (view === 'chat' && !sid && !window.location.hash) return
  const hash = buildHash(view, sid)
  if (window.location.hash !== hash) {
    window.history.replaceState(null, '', hash)
  }
}
