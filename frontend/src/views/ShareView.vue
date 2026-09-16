<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { fetchSharedSession } from '../api/share'
import { normalizeThinking } from '../store/sessions'
import MessageItem from './MessageItem.vue'
import type { ChatMessage, SharedSession } from '../types'

const props = defineProps<{ token: string }>()
const emit = defineEmits<{ (e: 'exit'): void }>()

const loading = ref(true)
const error = ref('')
const data = ref<SharedSession | null>(null)

// 把后端原始消息映射为前端 ChatMessage（与 store 一致：thinking 归一化为字符串）
function mapMessages(raw: unknown[]): ChatMessage[] {
  return (raw ?? []).map((m) => {
    const msg = m as Record<string, unknown>
    return {
      role: (msg.role as 'user' | 'assistant') ?? 'user',
      content: (msg.content as string) ?? '',
      items: (msg.items as ChatMessage['items']) ?? undefined,
      steps: (msg.steps as ChatMessage['steps']) ?? undefined,
      thinking: normalizeThinking(msg.thinking),
      intent: (msg.intent as string) ?? undefined,
      sources: (msg.sources as Record<string, unknown>) ?? undefined,
    }
  })
}

const messages = computed<ChatMessage[]>(() =>
  data.value ? mapMessages(data.value.messages ?? []) : []
)

onMounted(async () => {
  loading.value = true
  error.value = ''
  try {
    const r = await fetchSharedSession(props.token)
    data.value = r as SharedSession
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
})

function backToApp() {
  emit('exit')
}
</script>

<template>
  <div class="share-shell">
    <!-- 只读横幅：明确提示这是他人共享的会话，不可编辑 -->
    <header class="banner">
      <span class="tag">只读 · 他人共享</span>
      <span class="title">{{ data?.title || '共享会话' }}</span>
      <button class="back" @click="backToApp">返回应用</button>
    </header>

    <main class="read-only">
      <div v-if="loading" class="state">加载中…</div>
      <div v-else-if="error" class="state err">
        {{ error }}
        <div class="hint">链接可能已失效，或分享已被取消。</div>
      </div>
      <div v-else-if="messages.length === 0" class="state">该会话暂无内容。</div>
      <div v-else class="messages">
        <MessageItem v-for="(m, i) in messages" :key="i" :message="m" />
      </div>
    </main>
  </div>
</template>

<style scoped>
.share-shell {
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
  background: var(--bg);
}
.banner {
  flex: none; display: flex; align-items: center; gap: 12px;
  padding: 12px 20px; border-bottom: 1px solid var(--line); background: var(--surface);
}
.tag {
  font-size: 12px; color: var(--accent); background: var(--chip-bg);
  border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; flex: none;
}
.title { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.back {
  margin-left: auto; flex: none; background: var(--accent); color: var(--accent-fg);
  padding: 7px 14px; font-size: 13px; border-radius: 8px;
}
.read-only { flex: 1; min-height: 0; overflow-y: auto; }
.messages { max-width: 800px; margin: 0 auto; padding: 20px 16px 40px; }
.state {
  max-width: 480px; margin: 80px auto; text-align: center; color: var(--muted); font-size: 15px;
}
.state.err { color: var(--danger); }
.state .hint { margin-top: 8px; font-size: 13px; color: var(--muted); }
</style>
