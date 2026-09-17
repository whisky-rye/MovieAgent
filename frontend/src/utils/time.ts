// 时间格式化：输出绝对时间 YYYY-MM-DD HH:MM（本地时区），
// 用于会话列表条目与对话内每条子对话的展示；非法/空返回空串交由调用方占位。

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

// 输入 ISO 字符串（后端为 UTC，new Date 按本地时区解析出绝对日历时间），非法/空返回空串
export function fmtConvTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
