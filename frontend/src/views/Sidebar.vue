<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { vFocus } from '../focus'
import {
  sessionList, currentSessionId, loadSessions, newSession, selectSession,
  renameSession, deleteSession, shareSession, unshareSession,
  sessionsListLoading, sessionsListError,
} from '../store/sessions'
import { sidebarOpen, closeSidebar } from '../store/ui'

const renamingId = ref<string | null>(null)
const renameValue = ref('')
const busyId = ref<string | null>(null)   // 正在执行「共享/取消共享」的会话
const shareTip = ref('')                   // 复制链接后的轻提示
let tipTimer: ReturnType<typeof setTimeout> | null = null

const listLoading = sessionsListLoading
const listError = sessionsListError

onMounted(loadSessions)

async function onNew() {
  await newSession()
  closeSidebar()
}

function startRename(id: string, title: string) {
  renamingId.value = id
  renameValue.value = title
}

async function commitRename() {
  const id = renamingId.value
  const title = renameValue.value.trim()
  renamingId.value = null
  if (id && title) await renameSession(id, title)
}

async function onDelete(id: string, title: string) {
  if (window.confirm(`确定删除会话「${title}」？此操作不可撤销。`)) {
    await deleteSession(id)
  }
}

// 移动端点选会话后收起抽屉
function onSelect(id: string) {
  selectSession(id)
  closeSidebar()
}

async function onToggleShare(id: string) {
  if (busyId.value) return
  busyId.value = id
  try {
    const item = sessionList.value.find((s) => s.session_id === id)
    if (item?.shared) {
      await unshareSession(id)
    } else {
      const url = await shareSession(id)
      await copyText(url)
      showTip('已生成共享链接并复制')
    }
  } catch (e) {
    showTip(e instanceof Error ? e.message : '操作失败')
  } finally {
    busyId.value = null
  }
}

// 仅复制链接（已共享时出现的按钮）
async function onCopyLink(token: string | undefined) {
  if (!token) return
  const origin = (import.meta.env.VITE_PUBLIC_ORIGIN as string | undefined) ||
    window.location.origin
  await copyText(`${origin}/share/${token}`)
  showTip('链接已复制')
}

function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
  }
  return fallbackCopy(text)
}

function fallbackCopy(text: string): Promise<void> {
  return new Promise((resolve) => {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch { /* 忽略 */ }
    ta.remove()
    resolve()
  })
}

function showTip(msg: string) {
  shareTip.value = msg
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => (shareTip.value = ''), 3000)
}

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <aside class="sidebar" :class="{ open: sidebarOpen }">
    <button class="new" @click="onNew">+ 新建对话</button>
    <transition name="tipfade">
      <div v-if="shareTip" class="share-tip" role="status">{{ shareTip }}</div>
    </transition>

    <div v-if="listLoading" class="empty">
      <span class="mini-spinner"></span> 加载中…
    </div>
    <div v-else-if="listError" class="empty error">
      {{ listError }}。<button class="link retry" @click="loadSessions">重试</button>
    </div>
    <div v-else-if="sessionList.length === 0" class="empty">还没有会话，点击上方新建。</div>

    <ul class="list">
      <li
        v-for="s in sessionList"
        :key="s.session_id"
        class="item"
        :class="{ active: s.session_id === currentSessionId }"
        @click="onSelect(s.session_id)"
      >
        <template v-if="renamingId === s.session_id">
          <input
            v-model="renameValue"
            class="rename-input"
            v-focus
            @keyup.enter="commitRename"
            @blur="commitRename"
          />
        </template>
        <template v-else>
          <div class="item-main">
            <span class="title">{{ s.title }}</span>
            <span v-if="s.shared" class="badge shared">已共享</span>
            <span class="time">{{ fmtDate(s.updated_at) }}</span>
          </div>
          <div class="preview">{{ s.preview || '（空会话）' }}</div>
          <div class="actions">
            <button
              class="mini"
              :class="{ 'share-on': s.shared }"
              :disabled="busyId === s.session_id"
              @click.stop="onToggleShare(s.session_id)"
            >{{ s.shared ? '取消共享' : '共享' }}</button>
            <button v-if="s.shared" class="mini" @click.stop="onCopyLink(s.share_token)">复制链接</button>
            <button class="mini" @click.stop="startRename(s.session_id, s.title)">重命名</button>
            <button class="mini danger" @click.stop="onDelete(s.session_id, s.title)">删除</button>
          </div>
        </template>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar { width: 248px; flex: none; border-right: 1px solid var(--line); background: var(--surface); display: flex; flex-direction: column; height: 100%; }
.new {
  margin: 12px; background: var(--accent); color: var(--accent-fg); padding: 10px; font-weight: 600;
}
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 12px; }
.empty.error { color: var(--danger); }
.empty .retry { padding: 0 2px; font-size: 13px; }
.mini-spinner {
  display: inline-block; width: 12px; height: 12px; vertical-align: -2px; margin-right: 4px;
  border: 2px solid var(--line); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 1s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.list { list-style: none; margin: 0; padding: 0 8px 12px; overflow-y: auto; flex: 1; }
.item {
  padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent;
}
.item:hover { background: var(--surface-2); }
.item.active { background: var(--item-active-bg); border-color: var(--item-active-border); }
.item-main { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; }
.title { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge.shared { background: var(--chip-bg); color: var(--accent); font-size: 10px; padding: 1px 6px; border-radius: 999px; flex: none; }
.time { font-size: 11px; color: var(--muted); flex: none; }
.preview { font-size: 12px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.actions { margin-top: 6px; display: flex; gap: 8px; visibility: hidden; flex-wrap: wrap; }
.item:hover .actions, .item.active .actions { visibility: visible; }
.mini { background: none; color: var(--muted); font-size: 12px; padding: 0; }
.mini.share-on { color: var(--accent); }
.mini.danger { color: var(--danger); }
.rename-input { width: 100%; font-size: 14px; padding: 4px 6px; }
.share-tip {
  margin: 0 12px 8px; font-size: 12.5px; color: var(--accent);
  background: var(--chip-bg); border: 1px solid var(--line);
  border-radius: 8px; padding: 5px 10px; text-align: center;
}
.tipfade-enter-active, .tipfade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.tipfade-enter-from, .tipfade-leave-to { opacity: 0; transform: translateY(-4px); }

/* 移动端：侧边栏变抽屉，默认移出屏幕，open 时滑入 */
@media (max-width: 720px) {
  .sidebar {
    position: absolute; top: 0; bottom: 0; left: 0; z-index: 50;
    width: 80%; max-width: 300px;
    transform: translateX(-100%); transition: transform 0.22s ease;
    box-shadow: var(--shadow-lg);
  }
  .sidebar.open { transform: none; }
  .actions { visibility: visible; }
}
</style>
