// 会话时间格式化：以「对话任务最后结束的时间」（后端 sessions.updated_at）为准，
// 按今天/昨天/今年/往年输出清晰易读的中文格式，与列表整体视觉风格保持一致。

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function hm(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

// 输入 ISO 字符串（后端为 UTC，new Date 会按本地时区解析），非法/空返回空串交由调用方展示占位
export function fmtConvTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)

  if (sameDay(d, now)) return `今天 ${hm(d)}`
  if (sameDay(d, yesterday)) return `昨天 ${hm(d)}`
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${hm(d)}`
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hm(d)}`
}
