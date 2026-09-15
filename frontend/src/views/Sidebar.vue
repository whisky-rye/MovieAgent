<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { vFocus } from '../focus'
import {
  sessionList, currentSessionId, loadSessions, newSession, selectSession,
  renameSession, deleteSession,
} from '../store/sessions'

const renamingId = ref<string | null>(null)
const renameValue = ref('')

onMounted(loadSessions)

async function onNew() {
  await newSession()
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

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <aside class="sidebar">
    <button class="new" @click="onNew">+ 新建对话</button>

    <div v-if="sessionList.length === 0" class="empty">还没有会话，点击上方新建。</div>

    <ul class="list">
      <li
        v-for="s in sessionList"
        :key="s.session_id"
        class="item"
        :class="{ active: s.session_id === currentSessionId }"
        @click="selectSession(s.session_id)"
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
            <span class="time">{{ fmtDate(s.updated_at) }}</span>
          </div>
          <div class="preview">{{ s.preview || '（空会话）' }}</div>
          <div class="actions">
            <button class="mini" @click.stop="startRename(s.session_id, s.title)">重命名</button>
            <button class="mini danger" @click.stop="onDelete(s.session_id, s.title)">删除</button>
          </div>
        </template>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar { width: 248px; flex: none; border-right: 1px solid var(--line); background: #fff; display: flex; flex-direction: column; height: 100%; }
.new {
  margin: 12px; background: var(--accent); color: #fff; padding: 10px; font-weight: 600;
}
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 12px; }
.list { list-style: none; margin: 0; padding: 0 8px 12px; overflow-y: auto; flex: 1; }
.item {
  padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent;
}
.item:hover { background: #f3f4f6; }
.item.active { background: #eef2ff; border-color: #c7d2fe; }
.item-main { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; }
.title { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.time { font-size: 11px; color: var(--muted); flex: none; }
.preview { font-size: 12px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.actions { margin-top: 6px; display: flex; gap: 8px; visibility: hidden; }
.item:hover .actions, .item.active .actions { visibility: visible; }
.mini { background: none; color: var(--muted); font-size: 12px; padding: 0; }
.mini.danger { color: var(--danger); }
.rename-input { width: 100%; font-size: 14px; padding: 4px 6px; }
</style>
