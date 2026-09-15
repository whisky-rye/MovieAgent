<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  memoryPreferences, memoryHistory, memoryLoading, memorySaving, memoryError,
  loadMemory, addGenre, removeGenre, setNote, clearPreferences,
} from '../store/memory'

const newGenre = ref('')
const noteDraft = ref('')
// 每次加载完记忆后，用后端最新的 note 同步草稿
const noteSynced = ref(false)

onMounted(async () => {
  await loadMemory()
  noteDraft.value = memoryPreferences.value.note ?? ''
  noteSynced.value = true
})

async function onAddGenre() {
  const g = newGenre.value.trim()
  if (!g) return
  newGenre.value = ''
  try {
    await addGenre(g)
  } catch {
    /* 错误已写入 memoryError */
  }
}

async function onRemoveGenre(g: string) {
  try {
    await removeGenre(g)
  } catch {
    /* 错误已写入 memoryError */
  }
}

async function onSaveNote() {
  try {
    await setNote(noteDraft.value.trim())
  } catch {
    /* 错误已写入 memoryError */
  }
}

async function onClear() {
  if (!confirm('确定清空长期偏好吗？（历史记录会保留）')) return
  try {
    await clearPreferences()
    noteDraft.value = ''
  } catch {
    /* 错误已写入 memoryError */
  }
}

function fmtHistoryItem(it: unknown): string {
  if (it && typeof it === 'object') {
    const o = it as Record<string, unknown>
    const parts = [o.title, o.genre, o.type].filter(Boolean).map(String)
    return parts.length ? parts.join(' · ') : JSON.stringify(it)
  }
  return String(it)
}
</script>

<template>
  <div class="memory">
    <!-- 三层记忆模型总览 -->
    <section class="layers">
      <div class="layer">
        <span class="dot short"></span>
        <div>
          <h4>短期记忆</h4>
          <p>当前轮次上下文（本次提问与正在生成的回答），仅运行时持有，不持久化。</p>
        </div>
      </div>
      <div class="layer">
        <span class="dot session"></span>
        <div>
          <h4>会话记忆</h4>
          <p>单个会话的多轮问答，按 session_id 隔离，切换/刷新可恢复，互不串味。</p>
        </div>
      </div>
      <div class="layer">
        <span class="dot long"></span>
        <div>
          <h4>长期记忆</h4>
          <p>跨会话的用户偏好（喜欢的类型 / 备注），下方可管理，AI 推荐会据此加权。</p>
        </div>
      </div>
    </section>

    <!-- 长期偏好（可编辑） -->
    <section class="card">
      <h3>长期偏好 <span class="tag">跨会话共享</span></h3>

      <div v-if="memoryError" class="error">{{ memoryError }}</div>
      <div v-if="memoryLoading" class="hint">加载中…</div>

      <div class="field">
        <label>喜欢的类型</label>
        <div class="chips">
          <span v-for="g in memoryPreferences.genres ?? []" :key="g" class="chip">
            {{ g }}
            <button class="x" @click="onRemoveGenre(g)" :disabled="memorySaving">×</button>
          </span>
          <span v-if="!(memoryPreferences.genres ?? []).length" class="muted">还没有记录任何类型偏好</span>
        </div>
        <div class="add-row">
          <input
            v-model="newGenre"
            placeholder="例如：科幻 / 悬疑 / 动画"
            @keydown.enter.prevent="onAddGenre"
          />
          <button class="ghost" @click="onAddGenre" :disabled="memorySaving || !newGenre.trim()">添加</button>
        </div>
      </div>

      <div class="field">
        <label>偏好备注</label>
        <textarea
          v-model="noteDraft"
          rows="3"
          placeholder="记录你的观影口味、常看导演等，AI 会在推荐时参考"
        ></textarea>
        <button class="ghost" @click="onSaveNote" :disabled="memorySaving">保存备注</button>
      </div>

      <div class="actions">
        <button class="danger" @click="onClear" :disabled="memorySaving">清空长期偏好</button>
      </div>
    </section>

    <!-- 历史记录（只读） -->
    <section class="card">
      <h3>历史记录</h3>
      <div v-if="!memoryHistory.length" class="muted">暂无任何历史记录。</div>
      <ul v-else class="hist">
        <li v-for="(it, i) in memoryHistory" :key="i">{{ fmtHistoryItem(it) }}</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.memory { flex: 1; min-height: 0; padding: 24px; max-width: 760px; margin: 0 auto; overflow-y: auto; width: 100%; }

.layers { display: grid; gap: 12px; margin-bottom: 20px; }
.layer { display: flex; gap: 12px; align-items: flex-start; background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.layer h4 { margin: 0 0 4px; font-size: 14px; color: var(--fg); }
.layer p { margin: 0; font-size: 13px; line-height: 1.6; color: var(--muted); }
.dot { flex: none; width: 12px; height: 12px; border-radius: 50%; margin-top: 4px; }
.dot.short { background: #0ea5e9; }
.dot.session { background: var(--accent); }
.dot.long { background: #0f766e; }

.card { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
.card h3 { margin: 0 0 14px; font-size: 15px; color: var(--fg); display: flex; align-items: center; gap: 8px; }
.tag { font-size: 11px; font-weight: 400; color: var(--accent); background: #eef2ff; padding: 2px 8px; border-radius: 999px; }

.field { margin-bottom: 16px; }
.field label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.chip { display: inline-flex; align-items: center; gap: 4px; background: #eef2ff; color: var(--accent); border: 1px solid #dbe3ff; padding: 5px 10px; border-radius: 999px; font-size: 13px; }
.chip .x { background: none; color: var(--accent); padding: 0 2px; font-size: 15px; line-height: 1; }
.muted { color: var(--muted); font-size: 13px; }
.add-row { display: flex; gap: 8px; }
.add-row input { flex: 1; }
.add-row .ghost { flex: none; }

textarea { width: 100%; resize: vertical; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-family: inherit; font-size: 14px; color: var(--fg); }
textarea:focus, input:focus { border-color: var(--accent); outline: none; }
.ghost { background: #fff; color: var(--accent); border: 1px solid var(--line); padding: 0 14px; align-self: flex-start; }
.ghost:disabled { opacity: 0.5; cursor: default; }

.actions { display: flex; justify-content: flex-end; }
.danger { background: #fff; color: var(--danger); border: 1px solid var(--danger); padding: 0 14px; height: 36px; }
.danger:disabled { opacity: 0.5; cursor: default; }

.error { background: #fef2f2; color: var(--danger); border: 1px solid #fecaca; padding: 8px 12px; border-radius: 8px; font-size: 13px; margin-bottom: 12px; }
.hint { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
.hist { margin: 0; padding-left: 18px; color: var(--fg); font-size: 13px; line-height: 1.8; }

@media (max-width: 720px) {
  .memory { padding: 16px 14px; }
  .layers { grid-template-columns: 1fr; }
  .card { padding: 14px 16px; }
  .actions { justify-content: stretch; }
  .danger { width: 100%; }
}
</style>
