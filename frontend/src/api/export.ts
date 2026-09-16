// 会话历史导出：GET /export/sessions -> zip blob -> 生成下载链接触发浏览器下载。
// 与 client.ts 同范式：原生 fetch + credentials:'include'（httpOnly Cookie 鉴权）；
// GET 请求天然豁免 CSRF，无需带 X-CSRF-Token。
import { API_BASE } from './client'

export interface ExportResult {
  /** 触发下载使用的文件名（优先取后端 Content-Disposition，回退本地时间戳名） */
  filename: string
  /** 导出的会话条数（后端 X-Export-Sessions 头，可能为 null） */
  sessionCount: number | null
  /** zip 包体积（字节） */
  sizeBytes: number
}

/** 从 Content-Disposition 解析附件名：优先 RFC5987 filename*，回退 filename */
function parseFilename(disposition: string | null): string | null {
  if (!disposition) return null
  const star = disposition.match(/filename\*=(?:UTF-8'')?([^;]+)/i)
  if (star) {
    try {
      return decodeURIComponent(star[1].replace(/^"|"$/g, ''))
    } catch {
      /* fallthrough */
    }
  }
  const plain = disposition.match(/filename="([^"]+)"/i)
  return plain ? plain[1] : null
}

/** 触发浏览器下载一个 blob（临时 <a> 下载链接，用后即焚） */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/**
 * 导出当前用户全部会话：请求 zip -> 生成下载链接 -> 自动点击下载。
 * 返回导出结果供 UI 提示；失败抛 ApiError（含 status/detail）。
 */
export async function exportSessionsZip(): Promise<ExportResult> {
  const res = await fetch(`${API_BASE}/export/sessions`, {
    method: 'GET',
    credentials: 'include',
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail)
    } catch {
      /* keep statusText */
    }
    throw new Error(`导出失败（${res.status}）：${detail}`)
  }

  const blob = await res.blob()
  const filename =
    parseFilename(res.headers.get('Content-Disposition')) ??
    `chat_export_${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.zip`

  downloadBlob(blob, filename)

  const countHeader = res.headers.get('X-Export-Sessions')
  return {
    filename,
    sessionCount: countHeader === null ? null : Number(countHeader),
    sizeBytes: blob.size,
  }
}
