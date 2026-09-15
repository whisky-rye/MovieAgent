// 纯函数 SSE 解析（零 DOM / 零 fetch 依赖，便于单测）。
// 解析规则对齐后端 api/main.py 的 _sse()：每帧以空行 `\n\n` 分隔，
// 帧内 `event:` 指定事件名（缺省 message），`data:` 为载荷，多行 data 拼接（换行连接）。

export interface SSEEvent {
  event: string
  data: string
}

// 解析单个 SSE 帧文本（不含结尾空行）。无 data 的帧（如注释 `: keep-alive`）返回 null。
export function parseSSEBlock(block: string): SSEEvent | null {
  let event = 'message'
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      // data: 后可能带一个空格；trim 掉，多行 data 用换行连接
      data += (data ? '\n' : '') + line.slice(5).trim()
    }
    // 其他以冒号开头的行（如 `: comment`）忽略
  }
  if (!data) return null
  return { event, data }
}

// 状态化解析器：跨多次 chunk 累积缓冲，仅在遇到 `\n\n` 时产出完整帧。
// 解决「一个事件被网络分片拆到两个 read()」的边界问题。
export function createSSEParser() {
  let buf = ''
  return {
    push(chunk: string): SSEEvent[] {
      buf += chunk
      const out: SSEEvent[] = []
      let i: number
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, i)
        buf = buf.slice(i + 2)
        const ev = parseSSEBlock(block)
        if (ev) out.push(ev)
      }
      return out
    },
    // 流结束时把残留缓冲（可能最后一个帧没有结尾空行）强制产出
    flush(): SSEEvent[] {
      const rest = buf
      buf = ''
      if (!rest.trim()) return []
      const ev = parseSSEBlock(rest)
      return ev ? [ev] : []
    },
  }
}
