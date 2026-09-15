<script setup lang="ts">
import { ref, nextTick, onBeforeUnmount } from 'vue'
import MessageItem from './MessageItem.vue'
import { streamRecommend } from '../api/stream'
import {
  currentSessionId, currentMessages, newSession, pushMessage, updateLastAssistant,
} from '../store/sessions'
import type { ChatMessage, ChatStep, DonePayload } from '../types'

const input = ref('')
const streaming = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

async function scrollToBottom() {
  await nextTick()
  const el = scrollEl.value
  if (el) el.scrollTop = el.scrollHeight
}

async function ensureSession(): Promise<string> {
  let sid = currentSessionId.value
  if (!sid) sid = await newSession()
  return sid
}

async function send() {
  const q = input.value.trim()
  if (!q || streaming.value) return

  const sid = await ensureSession()

  // 立即插入用户消息 + 空的助手占位
  pushMessage({ role: 'user', content: q })
  pushMessage({ role: 'assistant', content: '', steps: [], streaming: true })
  input.value = ''
  await scrollToBottom()

  streaming.value = true
  controller = new AbortController()

  const steps: ChatStep[] = []
  try {
    await streamRecommend(
      q,
      sid,
      {
        onStep: (s) => {
          steps.push(s)
          updateLastAssistant((m) => {
            m.steps = [...steps]
          })
        },
        onThinking: (delta) => {
          updateLastAssistant((m) => {
            m.thinking = (m.thinking ?? '') + delta
          })
        },
        onToken: (delta) => {
          updateLastAssistant((m) => {
            m.content += delta
          })
          scrollToBottom()
        },
        onDone: (p: DonePayload) => {
          updateLastAssistant((m) => {
            m.content = p.answer
            m.items = p.items
            m.steps = p.steps
            m.thinking = typeof p.thinking === 'string' ? p.thinking : m.thinking
            m.intent = p.intent
            m.sources = p.sources
            m.streaming = false
          })
        },
        onError: (msg) => {
          updateLastAssistant((m) => {
            m.error = msg
            m.streaming = false
          })
        },
      },
      controller.signal,
    )
  } finally {
    streaming.value = false
    controller = null
  }
}

function stop() {
  controller?.abort()
  updateLastAssistant((m) => {
    m.streaming = false
  })
  streaming.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <div class="chat">
    <div v-if="currentMessages.length === 0" class="empty">
      <div class="empty-inner">
        <h2>想看点什么？</h2>
        <p>试试：「推荐几部赛博朋克风格的科幻片」「适合周末看的轻松喜剧」「诺兰导演的烧脑电影」</p>
      </div>
    </div>

    <div v-else ref="scrollEl" class="messages">
      <MessageItem v-for="(m, i) in currentMessages" :key="i" :message="m" />
    </div>

    <div class="composer">
      <textarea
        v-model="input"
        rows="1"
        placeholder="描述你想看的电影，Enter 发送 / Shift+Enter 换行"
        @keydown="onKey"
      ></textarea>
      <button v-if="!streaming" class="send" :disabled="!input.trim()" @click="send">发送</button>
      <button v-else class="stop" @click="stop">停止</button>
    </div>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.empty { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--muted); }
.empty-inner { max-width: 460px; text-align: center; }
.empty-inner h2 { margin: 0 0 8px; color: var(--fg); }
.empty-inner p { font-size: 14px; line-height: 1.7; }

.messages { flex: 1; min-height: 0; overflow-y: auto; padding: 20px 24px; }
.composer {
  display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--line); background: #fff;
}
.composer textarea {
  flex: 1; resize: none; border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; font-family: inherit; line-height: 1.5; max-height: 120px;
}
.composer textarea:focus { border-color: var(--accent); }
.send { background: var(--accent); color: #fff; padding: 0 18px; }
.send:disabled { opacity: 0.5; cursor: default; }
.stop { background: var(--danger); color: #fff; padding: 0 18px; }
</style>
