// 轻量 Markdown 渲染（零依赖）。
// 仅处理推荐场景最常见的语法：加粗、斜体、行内代码、代码块、无序/有序列表、分段。
// 不引入 marked 等库，以保持 bundle 极小。

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

export function renderMarkdown(src: string): string {
  if (!src) return ''
  // 先转义 HTML，再解析 markdown
  let html = escapeHtml(src)

  // 代码块
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => {
    const clean = code.replace(/^\n|\n$/g, '')
    return `<pre><code>${clean}</code></pre>`
  })

  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')

  // 加粗
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/__([^_]+)__/g, '<strong>$1</strong>')

  // 斜体
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  html = html.replace(/_([^_]+)_/g, '<em>$1</em>')

  // 段落：按空行分割
  const blocks = html.split(/\n\n+/)
  return blocks
    .map((block) => {
      const trimmed = block.trim()
      if (!trimmed) return ''
      // 列表
      if (/^[-*+]\s/.test(trimmed)) {
        const items = trimmed.split('\n').map((line) => {
          const m = line.match(/^[-*+]\s+(.*)$/)
          return m ? `<li>${m[1]}</li>` : line
        })
        return `<ul>${items.join('')}</ul>`
      }
      if (/^\d+\.\s/.test(trimmed)) {
        const items = trimmed.split('\n').map((line) => {
          const m = line.match(/^\d+\.\s+(.*)$/)
          return m ? `<li>${m[1]}</li>` : line
        })
        return `<ol>${items.join('')}</ol>`
      }
      // 普通段落（保留换行）
      return `<p>${trimmed.replace(/\n/g, '<br>')}</p>`
    })
    .filter(Boolean)
    .join('')
}
